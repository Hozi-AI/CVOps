"""AutoLabelStep — run local YOLO inference and write annotation_revision rows.

Layering: heavy ML deps (ultralytics, PIL, numpy) are lazy-imported inside
run() so this module stays import-safe in the API env, which does not have
them installed. The step is dispatched to the 'training' worker which has
the full ML stack.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path

from cvops_api.engine.step import Step, StepContext

with open(Path(__file__).parent / "schemas" / "auto_label.json") as f:
    _SCHEMA = json.load(f)


class AutoLabelStep(Step):
    type_key = "step.auto_label"
    config_schema = _SCHEMA
    # ponytail: worker-training has ultralytics/torch; preprocessing worker does not
    queue = "training"

    async def run(self, ctx: StepContext, config: dict, inputs: dict) -> dict:
        import io  # noqa: PLC0415

        import numpy as np  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415
        from sqlalchemy import text  # noqa: PLC0415
        from ultralytics import YOLO  # noqa: PLC0415

        session = ctx.session
        model_version_id = config["model_version_id"]
        threshold = float(config.get("confidence_threshold", 0.35))
        sample_ids = [str(s) for s in inputs.get("sample_ids", [])]

        if not sample_ids:
            return {"annotation_revision_ids": []}

        # ── Resolve model weights blob ──────────────────────────────────────
        mv_row = (
            await session.execute(
                text("SELECT blob_hash FROM model_versions WHERE id = CAST(:id AS uuid)"),
                {"id": model_version_id},
            )
        ).first()
        if mv_row is None:
            raise ValueError(f"model_version {model_version_id} not found")
        weights_blob_hash = mv_row[0]

        # ── Resolve samples ─────────────────────────────────────────────────
        sample_rows = (
            await session.execute(
                text(
                    "SELECT id, blob_hash, width, height FROM samples "
                    "WHERE id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": sample_ids},
            )
        ).all()
        if not sample_rows:
            raise ValueError(f"no samples found for ids: {sample_ids}")

        # ── Resolve ontology (config override or project default) ───────────
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
                raise ValueError(f"ontology {ont_id_cfg} not found")
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
                raise ValueError("auto_label requires the project to have an ontology")
        ont_id, ont_version = str(ont_row[0]), ont_row[1]

        # ── Download weights, load YOLO, infer per sample ───────────────────
        weights_bytes = await ctx.storage.get_bytes(weights_blob_hash)
        revision_ids: list[str] = []

        with tempfile.TemporaryDirectory() as tmp:
            pt_path = Path(tmp) / "model.pt"
            pt_path.write_bytes(weights_bytes)
            model = YOLO(str(pt_path))

            for sid, blob_hash, width, height in sample_rows:
                img_bytes = await ctx.storage.get_bytes(blob_hash)
                image = np.array(Image.open(io.BytesIO(img_bytes)).convert("RGB"))
                results = model(image, conf=threshold, verbose=False)[0]

                detections = []
                for box in results.boxes:
                    x1, y1, x2, y2 = box.xyxy[0].tolist()
                    detections.append(
                        {
                            "label": model.names[int(box.cls[0])],
                            "confidence": float(box.conf[0]),
                            # Normalize to [0,1] relative coords
                            "points": [
                                x1 / width,
                                y1 / height,
                                x2 / width,
                                y2 / height,
                            ],
                            "type": "rectangle",
                        }
                    )

                rev_no = (
                    await session.execute(
                        text(
                            "SELECT COALESCE(MAX(revision_no), 0) + 1 "
                            "FROM annotation_revisions WHERE sample_id = CAST(:sid AS uuid)"
                        ),
                        {"sid": str(sid)},
                    )
                ).scalar()

                rev_id = str(uuid.uuid4())
                await session.execute(
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
                        "payload": json.dumps(detections),
                        "prov": "model",
                    },
                )
                revision_ids.append(rev_id)

        await ctx.emit_event(
            actor_id=ctx.actor_id,
            actor_type="system",
            entity_type="run",
            entity_id=ctx.run_id,
            action="auto_label.completed",
            payload={
                "sample_count": len(revision_ids),
                "model_version_id": model_version_id,
            },
        )

        return {"annotation_revision_ids": revision_ids}
