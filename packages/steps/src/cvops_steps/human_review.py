"""human_review — push a working set of samples to CVAT for human annotation,
then park the workflow run at a gate until the review completes.

Push flow (see docs/services/worker-cvat.md):

  1. Resolve sample_ids (the dataset working set) and their pre-label annotation
     revisions (typically produced by step.auto_label upstream).
  2. Download each image and write a temp file named ``<sample_id>.jpg`` so the
     pull flow can map a CVAT frame back to its sample by filename, independent
     of frame ordering.
  3. Create a CVAT task, upload the images, and upload the pre-labels (converted
     from canonical normalized boxes to CVAT pixel rects by the cvat client).
  4. Insert a labeling_jobs row recording the push.
  5. Optionally register a CVAT completion webhook (when CVAT_WEBHOOK_TARGET is
     set); otherwise the worker's poll fallback reconciles completion.
  6. Raise GateException so the coordinator parks the run as 'waiting'. The
     gate_data lands in runs.output_refs["gate_data"], which the dashboard's
     GateResolutionBanner reads to render "Open in CVAT".

Layering: steps may only touch cvops_api.core/engine, not db.models or routers,
so DB access is parameterized raw SQL via ctx.session. The CVAT client and its
cvat_sdk dependency are imported lazily inside run() so this module stays
import-safe in the API env — where the client may be absent and only the step's
registration metadata (type_key, config_schema) is needed. The step itself only
runs on worker-cvat, which has the client installed.
"""

from __future__ import annotations

import logging

from cvops_api.engine.step import GateException, Step, StepContext

logger = logging.getLogger(__name__)


class HumanReviewStep(Step):
    type_key = "step.human_review"
    is_gate = True
    queue = "cvat"
    config_schema = {
        "type": "object",
        "properties": {
            "labeling_backend": {"type": "string", "default": "cvat"},
            "assignees": {"type": "array", "items": {"type": "string"}},
            "task_name_prefix": {"type": "string", "default": "review-"},
        },
    }

    async def run(self, ctx: StepContext, config: dict, inputs: dict) -> dict:
        import json  # noqa: PLC0415
        import os  # noqa: PLC0415
        import tempfile  # noqa: PLC0415
        import uuid  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        from sqlalchemy import text  # noqa: PLC0415

        # Lazy — keeps this module import-safe where cvat_sdk isn't installed.
        from cvops_cvat_client import (  # noqa: PLC0415
            ReviewImage,
            push_review_task,
            register_webhook,
        )

        session = ctx.session

        # Pre-labels are optional. Drop null/"None" entries (samples without a
        # committed revision) so they don't reach the uuid[] cast below — older
        # frozen runs may carry the literal string "None".
        revision_ids = [
            str(r)
            for r in inputs.get("annotation_revision_ids", [])
            if r is not None and str(r) not in ("None", "")
        ]

        # ── Resolve pre-labels: the latest provided revision per sample ──────
        # The highest revision_no wins when several revisions reference one
        # sample (e.g. model + an earlier human pass).
        prelabels: dict[str, list] = {}
        in_rev_ids: list[str] = []
        if revision_ids:
            rev_rows = (
                await session.execute(
                    text(
                        "SELECT id, sample_id, revision_no, payload "
                        "FROM annotation_revisions "
                        "WHERE id = ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": revision_ids},
                )
            ).all()
            best: dict[str, tuple[int, str, list]] = {}
            for rid, sid, rev_no, payload in rev_rows:
                sid = str(sid)
                cur = best.get(sid)
                if cur is None or rev_no > cur[0]:
                    best[sid] = (rev_no, str(rid), payload or [])
            for sid, (_, rid, payload) in best.items():
                prelabels[sid] = payload
                in_rev_ids.append(rid)

        # sample_ids may be passed explicitly (working set) or derived from the
        # pre-label revisions when only those are wired into the step.
        sample_ids = [str(s) for s in inputs.get("sample_ids", [])]
        if not sample_ids:
            sample_ids = list(prelabels.keys())
        if not sample_ids:
            raise ValueError(
                "human_review requires sample_ids or annotation_revision_ids in inputs"
            )

        # ── Image + dimensions per sample ───────────────────────────────────
        sample_rows = (
            await session.execute(
                text(
                    "SELECT id, blob_hash, width, height FROM samples "
                    "WHERE id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": sample_ids},
            )
        ).all()
        samples = {str(sid): (bh, w, h) for sid, bh, w, h in sample_rows}
        missing = [s for s in sample_ids if s not in samples]
        if missing:
            raise ValueError(f"samples not found: {missing}")

        # ── Label space ─────────────────────────────────────────────────────
        # The CVAT task's label classes come from the project ontology, so
        # reviewers annotate with the canonical set (not ad-hoc labels) and the
        # pull maps them straight back. Required: without an ontology there is
        # nowhere to store reviewed labels (annotation_revisions.ontology_id is
        # NOT NULL), so fail loudly rather than create a class-less task.
        ont_id = (
            await session.execute(
                text(
                    "SELECT o.id FROM ontologies o "
                    "JOIN projects p ON p.org_id = o.org_id "
                    "WHERE p.id = CAST(:pid AS uuid) AND o.deleted_at IS NULL "
                    "ORDER BY (p.default_ontology_id = o.id) DESC NULLS LAST, o.version DESC "
                    "LIMIT 1"
                ),
                {"pid": ctx.project_id},
            )
        ).scalar()
        if ont_id is None:
            raise ValueError(
                "human_review requires the project to have an ontology — its label "
                "classes define what reviewers can annotate in CVAT"
            )
        label_names = [
            r[0]
            for r in (
                await session.execute(
                    text(
                        "SELECT class_key FROM label_classes WHERE ontology_id = CAST(:oid AS uuid)"
                    ),
                    {"oid": str(ont_id)},
                )
            ).all()
        ]

        # The gate's DAG node id — needed so the pull flow can resume this exact
        # waiting child (parent_run_id + step_id), mirroring resolve_gate.
        step_node_id = (
            await session.execute(
                text("SELECT step_id FROM runs WHERE id = CAST(:r AS uuid)"),
                {"r": ctx.run_id},
            )
        ).scalar() or self.type_key

        # ── Download images, push the task + pre-labels to CVAT ──────────────
        task_name = f"{config.get('task_name_prefix', 'review-')}{ctx.run_id[:8]}"
        with tempfile.TemporaryDirectory() as tmp:
            images: list[ReviewImage] = []
            for sid in sample_ids:
                blob_hash, width, height = samples[sid]
                img_bytes = await ctx.storage.get_bytes(blob_hash)
                # Filename carries the sample_id so the pull flow maps frame →
                # sample by name rather than relying on frame order.
                path = Path(tmp) / f"{sid}.jpg"
                path.write_bytes(img_bytes)
                images.append(
                    ReviewImage(
                        path=path,
                        width=width,
                        height=height,
                        annotations=prelabels.get(sid, []),
                    )
                )
            pushed = push_review_task(task_name, images, label_names=label_names)

        # ── Register completion webhook if configured (else poll fallback) ───
        # Best-effort: the webhook only enables auto-resume on CVAT completion.
        # If it fails (e.g. CVAT API drift — task-scoped webhooks were removed in
        # newer CVAT), the gate must still be raised so the run parks at `waiting`
        # and surfaces the "Open in CVAT" link; completion is then resolved by the
        # poll fallback or manually. Never let it abort the push.
        target = os.environ.get("CVAT_WEBHOOK_TARGET")
        secret = os.environ.get("CVAT_WEBHOOK_SECRET")
        if target and secret:
            try:
                register_webhook(pushed["task_id"], target, secret)
            except Exception:
                logger.warning(
                    "CVAT webhook registration failed for task %s; review gate "
                    "still opens — resolve completion via poll/manual.",
                    pushed["task_id"],
                    exc_info=True,
                )

        # ── Record the push ─────────────────────────────────────────────────
        labeling_job_id = str(uuid.uuid4())
        await session.execute(
            text(
                "INSERT INTO labeling_jobs (id, project_id, run_id, step_id, "
                "cvat_project_id, cvat_task_id, cvat_job_ids, status, sample_count, "
                "annotation_revision_ids_in) VALUES "
                "(CAST(:id AS uuid), CAST(:pid AS uuid), CAST(:rid AS uuid), :step, "
                ":cpid, :ctid, CAST(:jobs AS jsonb), 'pushed', :n, CAST(:inrev AS jsonb))"
            ),
            {
                "id": labeling_job_id,
                "pid": ctx.project_id,
                "rid": ctx.run_id,
                "step": step_node_id,
                "cpid": None,
                "ctid": pushed["task_id"],
                "jobs": json.dumps(pushed["job_ids"]),
                "n": len(sample_ids),
                "inrev": json.dumps(in_rev_ids),
            },
        )

        await ctx.emit_event(
            actor_id=ctx.actor_id,
            actor_type="system",
            entity_type="labeling_job",
            entity_id=labeling_job_id,
            action="labeling.pushed",
            payload={
                "cvat_task_id": pushed["task_id"],
                "cvat_url": pushed["cvat_url"],
                "sample_count": len(sample_ids),
            },
        )

        # Park the run. gate_data is persisted to output_refs["gate_data"]; the
        # dashboard reads cvat_url from there to link into CVAT.
        raise GateException(
            {
                "labeling_job_id": labeling_job_id,
                "cvat_task_id": pushed["task_id"],
                "cvat_url": pushed["cvat_url"],
                # Ordered to match CVAT frame order (upload order), so the pull
                # flow maps frame index → sample without a schema change.
                "sample_ids": sample_ids,
            }
        )
