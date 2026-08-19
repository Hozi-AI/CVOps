"""human_review — push a working set of samples to a labeling backend for
human annotation, then park the workflow run at a gate until the review
completes.

The backend is selected by config["labeling_backend"] (default "cvat").
Registered backends: cvat, label_studio.

Push flow:
  1. Resolve sample_ids and their pre-label annotation revisions.
  2. Resolve the project ontology for the label space.
  3. Dispatch to the configured LabelingBackend.push().
  4. Insert a labeling_jobs row recording the push.
  5. Raise GateException so the coordinator parks the run as 'waiting'.
     The gate_data lands in runs.output_refs["gate_data"].

Layering: steps may only touch cvops_api.core/engine, not db.models or routers,
so DB access is parameterized raw SQL via ctx.session. Backend clients are
imported lazily inside their own modules so this module stays import-safe.
"""

from __future__ import annotations

import json
import logging
import uuid

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
            "ontology_id": {
                "type": "string",
                "description": "UUID of the ontology to use for labels and new revisions. "
                               "Defaults to the project's default ontology.",
            },
        },
    }

    def idempotency_key(self, config: dict, inputs: dict) -> str:
        # ponytail: human labeling is not deterministic — same inputs produce a new
        # task and new annotation_revision_ids each time. Reusing a prior output
        # would pass stale revision IDs to commit_dataset.
        return uuid.uuid4().hex

    async def run(self, ctx: StepContext, config: dict, inputs: dict) -> dict:
        from sqlalchemy import text  # noqa: PLC0415
        from cvops_steps.labeling_backends import get_backend  # noqa: PLC0415
        from cvops_steps.labeling_backends.base import ReviewSample  # noqa: PLC0415

        backend_name = config.get("labeling_backend", "cvat")
        backend = get_backend(backend_name)
        session = ctx.session

        # Pre-labels are optional. Drop null/"None" entries — older frozen runs
        # may carry the literal string "None".
        revision_ids = [
            str(r)
            for r in inputs.get("annotation_revision_ids", [])
            if r is not None and str(r) not in ("None", "")
        ]

        # ── Resolve pre-labels: the latest provided revision per sample ──────
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

        sample_ids = [str(s) for s in inputs.get("sample_ids", [])]
        if not sample_ids:
            sample_ids = list(prelabels.keys())
        if not sample_ids:
            raise ValueError(
                "human_review requires sample_ids or annotation_revision_ids in inputs"
            )

        # ── Resolve samples (now with modality) ──────────────────────────────
        sample_rows = (
            await session.execute(
                text(
                    "SELECT id, blob_hash, width, height, modality FROM samples "
                    "WHERE id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": sample_ids},
            )
        ).all()
        samples_map = {str(r.id): r for r in sample_rows}
        missing = [s for s in sample_ids if s not in samples_map]
        if missing:
            raise ValueError(f"samples not found: {missing}")

        # ── Ontology / label space ───────────────────────────────────────────
        ont_id_cfg = config.get("ontology_id")
        if ont_id_cfg:
            ont_row = (
                await session.execute(
                    text(
                        "SELECT id, version FROM ontologies "
                        "WHERE id = CAST(:oid AS uuid) AND deleted_at IS NULL"
                    ),
                    {"oid": ont_id_cfg},
                )
            ).first()
            if ont_row is None:
                raise ValueError(f"ontology {ont_id_cfg} not found or deleted")
            ont_id, ont_version = str(ont_row[0]), ont_row[1]
        else:
            ont_row = (
                await session.execute(
                    text(
                        "SELECT o.id, o.version FROM ontologies o "
                        "JOIN projects p ON p.org_id = o.org_id "
                        "WHERE p.id = CAST(:pid AS uuid) AND o.deleted_at IS NULL "
                        "ORDER BY (p.default_ontology_id = o.id) DESC NULLS LAST, o.version DESC "
                        "LIMIT 1"
                    ),
                    {"pid": ctx.project_id},
                )
            ).first()
            if ont_row is None:
                raise ValueError(
                    "human_review requires the project to have an ontology — its label "
                    "classes define what reviewers can annotate"
                )
            ont_id, ont_version = str(ont_row[0]), ont_row[1]

        label_names = [
            r[0]
            for r in (
                await session.execute(
                    text(
                        "SELECT class_key FROM label_classes WHERE ontology_id = CAST(:oid AS uuid)"
                    ),
                    {"oid": ont_id},
                )
            ).all()
        ]

        step_node_id = (
            await session.execute(
                text("SELECT step_id FROM runs WHERE id = CAST(:r AS uuid)"),
                {"r": ctx.run_id},
            )
        ).scalar() or self.type_key

        # ── Assemble ReviewSample list and dispatch to backend ────────────────
        task_name = f"{config.get('task_name_prefix', 'review-')}{ctx.run_id[:8]}"
        review_samples = [
            ReviewSample(
                sample_id=sid,
                blob_hash=samples_map[sid].blob_hash,
                width=samples_map[sid].width,
                height=samples_map[sid].height,
                modality=samples_map[sid].modality,
                pre_label_annotations=prelabels.get(sid, []),
            )
            for sid in sample_ids
        ]

        push_result = await backend.push(review_samples, label_names, task_name, ctx.storage)

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
                "cpid": push_result.get("project_id"),
                "ctid": push_result.get("task_id") or push_result["job_id"],
                "jobs": json.dumps(push_result.get("job_ids", [])),
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
                "backend": backend_name,
                "job_id": push_result["job_id"],
                "sample_count": len(sample_ids),
            },
        )

        gate = backend.gate_data(push_result)
        gate.update({
            "labeling_job_id": labeling_job_id,
            "sample_ids": sample_ids,
            "ontology_id": ont_id,
            "ontology_version": ont_version,
        })
        raise GateException(gate)
