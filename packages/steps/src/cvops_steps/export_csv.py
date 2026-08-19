"""export_csv — render a sensor commit into a labeled CSV archive."""
from __future__ import annotations
import csv
import io
import json
from pathlib import Path
from typing import Any

from cvops_api.engine.step import Step, StepContext

with open(Path(__file__).parent / "schemas" / "export_csv.json") as f:
    _SCHEMA = json.load(f)


def _merge_rows(
    window: list[dict],
    annotations: list[dict],
    split: str,
    sample_id: str,
) -> list[dict[str, Any]]:
    """Append label/split columns to each sensor row based on time-region annotations."""
    result = []
    for data_row in window:
        ts = float(data_row.get("timestamp_ms", 0))
        label = ""
        for ann in annotations:
            t_start = float(ann.get("time_start_ms", float("-inf")))
            t_end = float(ann.get("time_end_ms", float("inf")))
            if t_start <= ts <= t_end:
                label = ann.get("class_key", "")
                break
        result.append({**data_row, "label": label, "split": split, "_sample_id": sample_id})
    return result


class ExportCsvStep(Step):
    type_key = "step.export_csv"
    config_schema = _SCHEMA
    queue = ""

    async def run(self, ctx: StepContext, config: dict, inputs: dict) -> dict:
        from sqlalchemy import text  # noqa: PLC0415

        commit_id = inputs["commit_id"]
        include_unannotated = bool(config.get("include_unannotated", False))

        rows_q = (
            await ctx.session.execute(
                text(
                    """
                    SELECT s.id AS sample_id, s.blob_hash, cs.split,
                           ar.payload AS annotation_payload
                    FROM commit_samples cs
                    JOIN samples s ON s.id = cs.sample_id
                    LEFT JOIN annotation_revisions ar ON ar.id = cs.annotation_revision_id
                    WHERE cs.commit_id = CAST(:cid AS uuid)
                      AND s.modality = 'sensor'
                    """
                ),
                {"cid": commit_id},
            )
        ).fetchall()

        all_csv_rows: list[dict] = []
        fieldnames: list[str] = []

        for row in rows_q:
            annotations = row.annotation_payload or []
            if isinstance(annotations, str):
                annotations = json.loads(annotations)
            if not annotations and not include_unannotated:
                continue

            raw = await ctx.storage.get_bytes(row.blob_hash)
            window: list[dict] = json.loads(raw)
            merged = _merge_rows(window, annotations, row.split, str(row.sample_id))

            if merged and not fieldnames:
                fieldnames = list(merged[0].keys())

            all_csv_rows.extend(merged)

        buf = io.StringIO()
        if all_csv_rows:
            writer = csv.DictWriter(buf, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_csv_rows)

        blob_hash = await ctx.storage.save_bytes(buf.getvalue().encode("utf-8"), "text/csv")
        return {"export_blob_hash": blob_hash, "sample_count": len(rows_q)}
