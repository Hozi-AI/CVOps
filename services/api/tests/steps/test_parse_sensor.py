"""Tests for step.parse_sensor."""
from __future__ import annotations
import json
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock

from cvops_api.engine.step import StepContext
from cvops_steps.parse_sensor import ParseSensorStep, _load_records, _window_records

CSV_DATA = b"timestamp_ms,value\n0,1.0\n100,1.5\n200,2.0\n1000,3.0\n1100,3.5\n"


def test_load_records_csv():
    rows = _load_records(CSV_DATA, "csv")
    assert len(rows) == 5
    assert rows[0]["timestamp_ms"] == 0.0
    assert rows[0]["value"] == 1.0


def test_load_records_json():
    data = json.dumps([{"timestamp_ms": 0, "v": 1}, {"timestamp_ms": 500, "v": 2}]).encode()
    rows = _load_records(data, "json")
    assert len(rows) == 2


def test_load_records_auto_detects_json():
    data = json.dumps([{"t": 0}]).encode()
    rows = _load_records(data, "auto")
    assert len(rows) == 1


def test_window_records():
    rows = [{"timestamp_ms": float(i * 100)} for i in range(15)]
    windows = _window_records(rows, "timestamp_ms", window_ms=1000, overlap_ms=0)
    assert len(windows) == 2
    assert len(windows[0]) == 10  # t=0..900


def _make_ctx(blob_bytes: bytes):
    src_id = str(uuid.uuid4())
    proj_id = str(uuid.uuid4())

    source_row = MagicMock()
    source_row.id = src_id
    source_row.blob_hash = "sblobhash"
    source_row.project_id = proj_id

    session = AsyncMock()
    session.execute.side_effect = [
        MagicMock(first=MagicMock(return_value=source_row)),
        MagicMock(),  # UPDATE ingesting
        MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(),
        MagicMock(),  # UPDATE ready
    ]

    storage = AsyncMock()
    storage.get_bytes = AsyncMock(return_value=blob_bytes)
    storage.save_bytes = AsyncMock(side_effect=lambda b, ct: "h" + uuid.uuid4().hex[:8])

    ctx = MagicMock(spec=StepContext)
    ctx.session = session
    ctx.storage = storage
    ctx.project_id = proj_id
    ctx.run_id = str(uuid.uuid4())
    ctx.actor_id = str(uuid.uuid4())
    ctx.emit_event = AsyncMock()
    return ctx, src_id


@pytest.mark.asyncio
async def test_parse_sensor_produces_two_windows():
    ctx, src_id = _make_ctx(CSV_DATA)
    result = await ParseSensorStep().run(
        ctx, config={"window_ms": 1000}, inputs={"source_id": src_id}
    )
    assert "sample_ids" in result
    assert len(result["sample_ids"]) == 2
    assert ctx.storage.save_bytes.call_count == 2
