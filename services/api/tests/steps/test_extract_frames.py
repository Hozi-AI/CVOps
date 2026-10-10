"""Integration test for the extract_frames step.

Exercises the real path: a synthetic mp4 (generated with OpenCV) → S3Backend (moto) →
ExtractFramesStep.run() → samples/blobs rows in testcontainers Postgres.
No external binaries required — OpenCV handles both video generation and processing.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

import pytest
from moto import mock_aws
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from unittest.mock import patch

from cvops_api.config import settings
from cvops_api.core.storage import S3Backend
from cvops_api.engine.step import StepContext
from cvops_steps.extract_frames import ExtractFramesStep


def _moto_settings() -> tuple:
    return (
        patch.object(settings, "S3_ENDPOINT", None),
        patch.object(settings, "S3_REGION", "us-east-1"),
        patch.object(settings, "S3_ACCESS_KEY", "testing"),
        patch.object(settings, "S3_SECRET_KEY", "testing"),
        patch.object(settings, "S3_BUCKET", "test-bucket"),
        patch.object(settings, "S3_PUBLIC_ENDPOINT", ""),
    )


def _mocked_backend() -> S3Backend:
    import boto3

    boto3.client("s3").create_bucket(Bucket=settings.S3_BUCKET)
    return S3Backend()


def _make_test_video() -> bytes:
    """2s 64x64 synthetic video at 10fps → 20 frames; 1fps sampling yields ~2."""
    import cv2
    import numpy as np

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp_path = tmp.name

    writer = cv2.VideoWriter(tmp_path, cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (64, 64))
    for i in range(20):
        frame = np.zeros((64, 64, 3), dtype=np.uint8)
        frame[:, :, i % 3] = min(255, 50 + i * 10)
        writer.write(frame)
    writer.release()

    data = Path(tmp_path).read_bytes()
    Path(tmp_path).unlink(missing_ok=True)
    return data


async def test_extract_frames_creates_samples(session: AsyncSession) -> None:
    video = _make_test_video()
    org_id, proj_id, ds_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    s1, s2, s3, s4, s5, s6 = _moto_settings()
    with mock_aws(), s1, s2, s3, s4, s5, s6:
        backend = _mocked_backend()
        source_hash = await backend.save_bytes(video, "video/mp4")

        await session.execute(
            text("INSERT INTO orgs (id, name) VALUES (:i, :n)"),
            {"i": org_id, "n": f"org-{uuid.uuid4().hex[:8]}"},
        )
        await session.execute(
            text("INSERT INTO projects (id, org_id, name) VALUES (:i, :o, :n)"),
            {"i": proj_id, "o": org_id, "n": f"proj-{uuid.uuid4().hex[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO blobs (hash, storage_backend, storage_key, "
                "size_bytes, media_type) VALUES (:h, 'garage', :k, :s, 'video/mp4')"
            ),
            {"h": source_hash, "k": f"blobs/{source_hash[7:9]}", "s": len(video)},
        )
        await session.execute(
            text(
                "INSERT INTO data_sources (id, project_id, type, blob_hash, status) "
                "VALUES (:i, :p, 'video', :h, 'uploaded')"
            ),
            {"i": ds_id, "p": proj_id, "h": source_hash},
        )
        await session.flush()

        ctx = StepContext(
            session=session,
            storage=backend,
            project_id=str(proj_id),
            run_id=str(uuid.uuid4()),
            actor_id=str(uuid.uuid4()),
            emit_event=lambda *a, **k: None,
        )
        result = await ExtractFramesStep().run(
            ctx, {"interval_seconds": 1.0}, {"source_id": str(ds_id)}
        )

    assert result["frame_count"] >= 1
    assert len(result["sample_ids"]) == result["frame_count"]

    count = (
        await session.execute(
            text("SELECT count(*) FROM samples WHERE project_id = CAST(:p AS uuid)"),
            {"p": str(proj_id)},
        )
    ).scalar()
    assert count == result["frame_count"]

    status = (
        await session.execute(
            text("SELECT status FROM data_sources WHERE id = CAST(:i AS uuid)"),
            {"i": str(ds_id)},
        )
    ).scalar()
    assert status == "ingested"

    # Every sample gets a thumbnail blob.
    no_thumb = (
        await session.execute(
            text(
                "SELECT count(*) FROM samples WHERE project_id = CAST(:p AS uuid) "
                "AND thumbnail_hash IS NULL"
            ),
            {"p": str(proj_id)},
        )
    ).scalar()
    assert no_thumb == 0


async def test_extract_frames_marks_source_failed(session: AsyncSession) -> None:
    """A step error must leave the source in a terminal 'failed' state, not
    silently revert it to a non-terminal status when the run is rolled back."""
    org_id, proj_id, ds_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    await session.execute(
        text("INSERT INTO orgs (id, name) VALUES (:i, :n)"),
        {"i": org_id, "n": f"org-{uuid.uuid4().hex[:8]}"},
    )
    await session.execute(
        text("INSERT INTO projects (id, org_id, name) VALUES (:i, :o, :n)"),
        {"i": proj_id, "o": org_id, "n": f"proj-{uuid.uuid4().hex[:8]}"},
    )
    # No blob → the step raises before it can extract anything.
    await session.execute(
        text(
            "INSERT INTO data_sources (id, project_id, type, status) "
            "VALUES (:i, :p, 'video', 'uploaded')"
        ),
        {"i": ds_id, "p": proj_id},
    )
    # Must be committed: the step rolls the session back on failure, and the
    # source has to survive that rollback to be flipped to 'failed'.
    await session.commit()

    ctx = StepContext(
        session=session,
        storage=None,
        project_id=str(proj_id),
        run_id=str(uuid.uuid4()),
        actor_id=str(uuid.uuid4()),
        emit_event=lambda *a, **k: None,
    )

    with pytest.raises(ValueError):
        await ExtractFramesStep().run(ctx, {"interval_seconds": 1.0}, {"source_id": str(ds_id)})

    status = (
        await session.execute(
            text("SELECT status FROM data_sources WHERE id = CAST(:i AS uuid)"),
            {"i": str(ds_id)},
        )
    ).scalar()
    assert status == "failed"
