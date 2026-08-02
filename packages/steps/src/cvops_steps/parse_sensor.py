"""parse_sensor — split a sensor capture into windowed samples.

Reads a CSV or JSON time-series file, partitions rows into fixed-length
time windows, stores each window as a JSON blob, and inserts one sample
per window with modality='sensor'.
"""
from __future__ import annotations
import csv
import io
import json
import uuid
from pathlib import Path
from typing import Any

from cvops_api.engine.step import Step, StepContext

with open(Path(__file__).parent / "schemas" / "parse_sensor.json") as f:
    _SCHEMA = json.load(f)


def _load_records(data: bytes, fmt: str) -> list[dict[str, Any]]:
    if fmt == "auto":
        stripped = data.lstrip()
        fmt = "json" if (stripped.startswith(b"[") or stripped.startswith(b"{")) else "csv"
    if fmt == "json":
        parsed = json.loads(data)
        return parsed if isinstance(parsed, list) else [parsed]
    reader = csv.DictReader(io.StringIO(data.decode("utf-8", errors="replace")))
    result = []
    for row in reader:
        coerced: dict[str, Any] = {}
        for k, v in row.items():
            try:
                coerced[k] = float(v)
            except (ValueError, TypeError):
                coerced[k] = v
        result.append(coerced)
    return result


def _window_records(
    records: list[dict],
    ts_col: str,
    window_ms: int,
    overlap_ms: int,
) -> list[list[dict]]:
    if not records:
        return []
    ts_values = [float(r.get(ts_col, 0)) for r in records]
    t_start = ts_values[0]
    t_end = ts_values[-1]
    step = max(1, window_ms - overlap_ms)
    windows = []
    t = t_start
    while t <= t_end:
        window = [r for r, ts in zip(records, ts_values) if t <= ts < t + window_ms]
        if window:
            windows.append(window)
        t += step
    return windows


class ParseSensorStep(Step):
    type_key = "step.parse_sensor"
    config_schema = _SCHEMA
    queue = ""

    async def run(self, ctx: StepContext, config: dict, inputs: dict) -> dict:
        from sqlalchemy import text  # noqa: PLC0415

        source_id = inputs["source_id"]
        window_ms = int(config.get("window_ms", 1000))
        overlap_ms = int(config.get("overlap_ms", 0))
        ts_col = config.get("timestamp_col", "timestamp_ms")
        fmt = config.get("format", "auto")

        row = (
            await ctx.session.execute(
                text(
                    "SELECT id, blob_hash, project_id FROM data_sources "
                    "WHERE id = CAST(:id AS uuid)"
                ),
                {"id": source_id},
            )
        ).first()
        if row is None:
            raise ValueError(f"data_source {source_id} not found")

        await ctx.session.execute(
            text("UPDATE data_sources SET status='ingesting' WHERE id = CAST(:id AS uuid)"),
            {"id": source_id},
        )

        raw = await ctx.storage.get_bytes(row.blob_hash)
        records = _load_records(raw, fmt)
        windows = _window_records(records, ts_col, window_ms, overlap_ms)

        sample_ids: list[str] = []
        for i, window in enumerate(windows):
            blob_bytes = json.dumps(window).encode("utf-8")
            blob_hash = await ctx.storage.save_bytes(blob_bytes, "application/json")
            sid = str(uuid.uuid4())

            ts_start = float(window[0].get(ts_col, 0))
            ts_end = float(window[-1].get(ts_col, 0))
            await ctx.session.execute(
                text(
                    "INSERT INTO samples "
                    "(id, project_id, blob_hash, source_id, modality, width, height, "
                    " frame_index, metadata_) "
                    "VALUES (CAST(:id AS uuid), CAST(:pid AS uuid), :bh, "
                    "        CAST(:src AS uuid), 'sensor', NULL, NULL, :fi, :meta::jsonb) "
                    "ON CONFLICT (project_id, blob_hash) DO NOTHING"
                ),
                {
                    "id": sid,
                    "pid": ctx.project_id,
                    "bh": blob_hash,
                    "src": source_id,
                    "fi": i,
                    "meta": json.dumps(
                        {
                            "window_index": i,
                            "row_count": len(window),
                            "ts_start_ms": ts_start,
                            "ts_end_ms": ts_end,
                        }
                    ),
                },
            )
            sample_ids.append(sid)

        await ctx.session.execute(
            text("UPDATE data_sources SET status='ready' WHERE id = CAST(:id AS uuid)"),
            {"id": source_id},
        )
        return {"sample_ids": sample_ids}
