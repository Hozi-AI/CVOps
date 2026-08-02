"""AutoLabelStep — run model inference and write annotation_revision rows.

Dispatched to the 'training' worker (which has the full ML stack).
The concrete runner is chosen by config["model_runner"] (default: "yolo").
Heavy deps stay in the runner implementation, not here.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from cvops_api.engine.step import Step, StepContext

with open(Path(__file__).parent / "schemas" / "auto_label.json") as f:
    _SCHEMA = json.load(f)


class AutoLabelStep(Step):
    type_key = "step.auto_label"
    config_schema = _SCHEMA
    queue = "training"

    async def run(self, ctx: StepContext, config: dict, inputs: dict) -> dict:
        from sqlalchemy import text  # noqa: PLC0415

        from cvops_steps.model_runners import get_runner  # noqa: PLC0415

        model_version_id = config["model_version_id"]
        threshold = float(config.get("confidence_threshold", 0.35))
        sample_ids = [str(s) for s in inputs.get("sample_ids", [])]

        if not sample_ids:
            return {"annotation_revision_ids": []}

        runner_name = config.get("model_runner", "yolo")
        runner = get_runner(runner_name)

        # Resolve model weights
        mv_row = (
            await ctx.session.execute(
                text("SELECT blob_hash FROM model_versions WHERE id = CAST(:id AS uuid)"),
                {"id": model_version_id},
            )
        ).first()
        if mv_row is None:
            raise ValueError(f"model_version {model_version_id} not found")
        model_bytes = await ctx.storage.get_bytes(mv_row[0])

        # Fetch samples (including modality)
        sample_rows = (
            await ctx.session.execute(
                text(
                    "SELECT id, blob_hash, modality FROM samples "
                    "WHERE id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": sample_ids},
            )
        ).fetchall()

        # Resolve ontology
        ont_id_cfg = config.get("ontology_id")
        if ont_id_cfg:
            ont_row = (
                await ctx.session.execute(
                    text(
                        "SELECT id, version FROM ontologies "
                        "WHERE id = CAST(:oid AS uuid) AND deleted_at IS NULL"
                    ),
                    {"oid": ont_id_cfg},
                )
            ).first()
            if ont_row is None:
                raise ValueError(f"ontology {ont_id_cfg} not found")
        else:
            ont_row = (
                await ctx.session.execute(
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
                raise ValueError("auto_label requires the project to have an ontology")
        ont_id, ont_version = str(ont_row[0]), ont_row[1]

        revision_ids: list[str] = []
        for sid, blob_hash, modality in sample_rows:
            annotations = await runner.predict(
                sample_id=str(sid),
                blob_hash=blob_hash,
                modality=modality,
                model_bytes=model_bytes,
                config={**config, "confidence_threshold": threshold},
                storage=ctx.storage,
            )
            if not annotations:
                continue

            rev_no = (
                await ctx.session.execute(
                    text(
                        "SELECT COALESCE(MAX(revision_no), 0) + 1 "
                        "FROM annotation_revisions WHERE sample_id = CAST(:sid AS uuid)"
                    ),
                    {"sid": str(sid)},
                )
            ).scalar()

            annotation_type = (
                "annotation.cv.detection"
                if modality == "image"
                else "annotation.text.classification"
            )
            rev_id = str(uuid.uuid4())
            await ctx.session.execute(
                text(
                    "INSERT INTO annotation_revisions "
                    "(id, sample_id, revision_no, ontology_id, ontology_version, "
                    "payload, provenance) VALUES "
                    "(CAST(:id AS uuid), CAST(:sid AS uuid), :rev_no, "
                    "CAST(:oid AS uuid), :over, CAST(:payload AS jsonb), :prov)"
                ),
                {
                    "id": rev_id,
                    "sid": str(sid),
                    "rev_no": rev_no,
                    "oid": ont_id,
                    "over": ont_version,
                    "payload": json.dumps(annotations),
                    "prov": json.dumps({"source": "auto_label", "runner": runner_name}),
                },
            )
            revision_ids.append(rev_id)

        await ctx.emit_event(
            actor_id=ctx.actor_id,
            actor_type="system",
            entity_type="run",
            entity_id=ctx.run_id,
            action="auto_label.completed",
            payload={"sample_count": len(revision_ids), "model_version_id": model_version_id},
        )

        return {"annotation_revision_ids": revision_ids}
