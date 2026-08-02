"""chunk_text — split a text blob into sample-sized chunks.

Reads a UTF-8 text blob from the data source, splits by the configured
strategy, uploads each chunk as its own content-addressed blob, and
inserts one samples row per chunk with modality='text'.
"""
from __future__ import annotations
import json
import re
import uuid
from pathlib import Path

from cvops_api.engine.step import Step, StepContext

with open(Path(__file__).parent / "schemas" / "chunk_text.json") as f:
    _SCHEMA = json.load(f)


def _split_chars(text: str, size: int, overlap: int) -> list[str]:
    chunks, i = [], 0
    step = max(1, size - overlap)
    while i < len(text):
        chunks.append(text[i : i + size])
        i += step
    return [c for c in chunks if c.strip()]


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p for p in parts if p.strip()]


def _split_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


class ChunkTextStep(Step):
    type_key = "step.chunk_text"
    config_schema = _SCHEMA
    queue = ""

    async def run(self, ctx: StepContext, config: dict, inputs: dict) -> dict:
        from sqlalchemy import text  # noqa: PLC0415

        source_id = inputs["source_id"]
        chunk_size = int(config.get("chunk_size", 512))
        overlap = int(config.get("overlap", 64))
        split_by = config.get("split_by", "sentences")

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
        text_content = raw.decode("utf-8", errors="replace")

        if split_by == "sentences":
            chunks = _split_sentences(text_content)
        elif split_by == "lines":
            chunks = _split_lines(text_content)
        else:
            chunks = _split_chars(text_content, chunk_size, overlap)

        sample_ids: list[str] = []
        for i, chunk in enumerate(chunks):
            chunk_bytes = chunk.encode("utf-8")
            blob_hash = await ctx.storage.save_bytes(chunk_bytes, "text/plain")
            sid = str(uuid.uuid4())
            await ctx.session.execute(
                text(
                    "INSERT INTO samples "
                    "(id, project_id, blob_hash, source_id, modality, width, height, "
                    " frame_index, metadata_) "
                    "VALUES (CAST(:id AS uuid), CAST(:pid AS uuid), :bh, "
                    "        CAST(:src AS uuid), 'text', NULL, NULL, :fi, :meta::jsonb) "
                    "ON CONFLICT (project_id, blob_hash) DO NOTHING"
                ),
                {
                    "id": sid,
                    "pid": ctx.project_id,
                    "bh": blob_hash,
                    "src": source_id,
                    "fi": i,
                    "meta": json.dumps({"char_count": len(chunk), "chunk_index": i}),
                },
            )
            sample_ids.append(sid)

        await ctx.session.execute(
            text("UPDATE data_sources SET status='ready' WHERE id = CAST(:id AS uuid)"),
            {"id": source_id},
        )
        return {"sample_ids": sample_ids}
