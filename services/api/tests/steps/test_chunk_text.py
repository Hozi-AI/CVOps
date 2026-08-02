"""Tests for step.chunk_text."""
from __future__ import annotations
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock

from cvops_api.engine.step import StepContext
from cvops_steps.chunk_text import ChunkTextStep, _split_chars, _split_sentences, _split_lines


def test_split_chars_basic():
    chunks = _split_chars("abcdefghij", size=4, overlap=0)
    assert chunks == ["abcd", "efgh", "ij"]


def test_split_chars_overlap():
    chunks = _split_chars("abcdefgh", size=4, overlap=2)
    # step = 2: "abcd", "cdef", "efgh", "gh"
    assert chunks[0] == "abcd"
    assert chunks[1] == "cdef"


def test_split_sentences():
    text = "Hello world. This is a test. Another one here."
    parts = _split_sentences(text)
    assert len(parts) == 3
    assert parts[0] == "Hello world."


def test_split_lines():
    text = "line one\nline two\n\nline three"
    parts = _split_lines(text)
    assert len(parts) == 3


def _make_ctx(blob_bytes: bytes):
    src_id = str(uuid.uuid4())
    proj_id = str(uuid.uuid4())
    blob_hash = "testhash"

    source_row = MagicMock()
    source_row.id = src_id
    source_row.blob_hash = blob_hash
    source_row.project_id = proj_id

    session = AsyncMock()
    session.execute.side_effect = [
        MagicMock(first=MagicMock(return_value=source_row)),
        MagicMock(),  # UPDATE status ingesting
        MagicMock(),  # INSERT samples (repeated N times — side_effect covers first)
        MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(),
        MagicMock(),  # UPDATE status ready
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
async def test_chunk_text_by_chars():
    text = b"a" * 100
    ctx, src_id = _make_ctx(text)
    result = await ChunkTextStep().run(
        ctx, config={"chunk_size": 20, "overlap": 0, "split_by": "chars"}, inputs={"source_id": src_id}
    )
    assert "sample_ids" in result
    assert len(result["sample_ids"]) == 5  # 100 / 20
    assert ctx.storage.save_bytes.call_count == 5


@pytest.mark.asyncio
async def test_chunk_text_by_sentences():
    text = b"First sentence. Second sentence. Third one here."
    ctx, src_id = _make_ctx(text)
    result = await ChunkTextStep().run(
        ctx, config={"split_by": "sentences"}, inputs={"source_id": src_id}
    )
    assert len(result["sample_ids"]) == 3
