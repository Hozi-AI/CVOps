"""export_jsonl — render a text commit into a JSONL dataset archive."""
from __future__ import annotations
import io
import json
from pathlib import Path
from typing import Any

from cvops_api.engine.step import Step, StepContext

with open(Path(__file__).parent / "schemas" / "export_jsonl.json") as f:
    _SCHEMA = json.load(f)


def _to_record(
    sample_id: str,
    text: str,
    split: str,
    annotations: list[dict],
    fmt: str,
) -> dict[str, Any]:
    if fmt == "openai":
        label = annotations[0]["class_key"] if annotations else "unknown"
        return {
            "messages": [
                {"role": "user", "content": text},
                {"role": "assistant", "content": label},
            ]
        }
    if fmt == "hf":
        label = annotations[0]["class_key"] if annotations else ""
        return {"text": text, "label": label}
    return {"id": sample_id, "text": text, "split": split, "labels": annotations}


class ExportJsonlStep(Step):
    type_key = "step.export_jsonl"
    config_schema = _SCHEMA
    queue = ""

    async def run(self, ctx: StepContext, config: dict, inputs: dict) -> dict:
        from sqlalchemy import text  # noqa: PLC0415

        commit_id = inputs["commit_id"]
        fmt = config.get("format", "raw")

        rows = (
            await ctx.session.execute(
                text(
                    """
                    SELECT s.id AS sample_id, s.blob_hash, cs.split,
                           ar.payload AS annotation_payload
                    FROM commit_samples cs
                    JOIN samples s ON s.id = cs.sample_id
                    LEFT JOIN annotation_revisions ar ON ar.id = cs.annotation_revision_id
                    WHERE cs.commit_id = CAST(:cid AS uuid)
                      AND s.modality = 'text'
                    """
                ),
                {"cid": commit_id},
            )
        ).fetchall()

        buf = io.BytesIO()
        for row in rows:
            raw_text = (await ctx.storage.get_bytes(row.blob_hash)).decode(
                "utf-8", errors="replace"
            )
            annotations = row.annotation_payload or []
            if isinstance(annotations, str):
                annotations = json.loads(annotations)
            record = _to_record(str(row.sample_id), raw_text, row.split, annotations, fmt)
            buf.write(json.dumps(record, ensure_ascii=False).encode("utf-8"))
            buf.write(b"\n")

        blob_hash = await ctx.storage.save_bytes(buf.getvalue(), "application/jsonl")
        return {"export_blob_hash": blob_hash, "sample_count": len(rows)}
