"""Integration test for the train step's blob registration.

The train step uploads the trained weights as a content-addressed blob and then
writes a ``ModelVersion`` whose ``blob_hash`` FKs ``blobs.hash``. ``save_bytes``
only puts the object into S3 — it does NOT insert a ``blobs`` row — so the step
must register the row itself or the ModelVersion insert violates
``fk_model_versions_blob_hash`` (the bug this guards against).

We seed project/ontology/dataset/commit, write a weights dir, drive
``_upload_weights`` (against moto S3) + ``_write_model_version``, and assert the
blob is registered and the ModelVersion insert succeeds (FK resolves).
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from unittest.mock import patch

from moto import mock_aws
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.config import settings
from cvops_api.core.storage import S3Backend
from cvops_api.engine.step import StepContext
from cvops_steps.train import _run_training, _upload_weights, _write_model_version


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


async def _emit(**kw):
    return None


async def _seed_commit(session: AsyncSession) -> tuple[str, str]:
    """Seed org/project/ontology/dataset + one commit. Returns (project_id, commit_id)."""
    org_id, proj_id, ont_id, ds_id, commit_id = (uuid.uuid4() for _ in range(5))
    await session.execute(
        text("INSERT INTO orgs (id, name) VALUES (:i, :n)"),
        {"i": org_id, "n": f"org-{uuid.uuid4().hex[:8]}"},
    )
    await session.execute(
        text("INSERT INTO projects (id, org_id, name) VALUES (:i, :o, :n)"),
        {"i": proj_id, "o": org_id, "n": f"proj-{uuid.uuid4().hex[:8]}"},
    )
    await session.execute(
        text("INSERT INTO ontologies (id, org_id, name, version) VALUES (:i, :o, 'o', 1)"),
        {"i": ont_id, "o": org_id},
    )
    await session.execute(
        text("INSERT INTO datasets (id, project_id, name) VALUES (:i, :p, 'ds')"),
        {"i": ds_id, "p": proj_id},
    )
    await session.execute(
        text(
            "INSERT INTO commits (id, project_id, dataset_id, ontology_id, "
            "ontology_version, message) VALUES (:i, :p, :d, :o, 1, 'c')"
        ),
        {"i": commit_id, "p": proj_id, "d": ds_id, "o": ont_id},
    )
    await session.flush()
    return str(proj_id), str(commit_id)


async def test_train_registers_weights_blob_and_writes_model_version(
    session: AsyncSession, tmp_path: Path
) -> None:
    s1, s2, s3, s4, s5, s6 = _moto_settings()
    with mock_aws(), s1, s2, s3, s4, s5, s6:
        backend = _mocked_backend()
        proj_id, commit_id = await _seed_commit(session)

        # Lay out a weights/ dir like the trainer produces under OUTPUT_DIR.
        output_dir = tmp_path / "output"
        weights_dir = output_dir / "weights"
        weights_dir.mkdir(parents=True)
        (weights_dir / "best.pt").write_bytes(b"fake-weights-payload")

        ctx = StepContext(
            session=session, storage=backend, project_id=proj_id,
            run_id=str(uuid.uuid4()), actor_id=str(uuid.uuid4()), emit_event=_emit,
        )

        blob_hash = await _upload_weights(ctx, output_dir, "/output/weights/")

        # The blobs row must exist — otherwise the ModelVersion FK fails.
        row = (
            await session.execute(
                text(
                    "SELECT storage_backend, storage_key, size_bytes, media_type "
                    "FROM blobs WHERE hash = :h"
                ),
                {"h": blob_hash},
            )
        ).first()
        assert row is not None, "weights blob was not registered in blobs"
        assert row.storage_backend == settings.S3_BACKEND
        assert row.media_type == "application/x-tar"
        assert row.size_bytes > 0

        # ModelVersion insert resolves fk_model_versions_blob_hash (the regression).
        mv_id = await _write_model_version(
            ctx, None, commit_id, blob_hash,
            {"map50_95": 0.06, "mlflow_run_id": "abc123"},
            {"epochs": 1, "seed": 7},
        )
        await session.flush()

        mv = (
            await session.execute(
                text(
                    "SELECT blob_hash, trained_on_commit_id, mlflow_run_id, seed, "
                    "metrics FROM model_versions WHERE id = CAST(:i AS uuid)"
                ),
                {"i": mv_id},
            )
        ).first()
        assert mv is not None
        assert mv.blob_hash == blob_hash
        assert str(mv.trained_on_commit_id) == commit_id
        assert mv.mlflow_run_id == "abc123"
        assert mv.seed == 7
        # mlflow_run_id is stripped out of the stored metrics blob.
        assert "mlflow_run_id" not in mv.metrics


async def test_run_training_streams_lines_and_forwards_markers(tmp_path: Path) -> None:
    """_run_training streams stdout (logged live) and forwards CVOPS_MLFLOW_*
    markers to the callback the moment they print — that's what lets the
    dashboard link to MLflow before the run finishes."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "t.py").write_text(
        "print('CVOPS_MLFLOW_RUN_ID=run-xyz', flush=True)\n"
        "print('CVOPS_MLFLOW_EXPERIMENT_ID=7', flush=True)\n"
        "print('epoch 1 done', flush=True)\n"
    )

    seen: list[tuple[str, str]] = []

    async def _on_marker(key: str, value: str) -> None:
        seen.append((key, value))

    rc, logs = await _run_training(
        repo, "t.py", {**__import__("os").environ}, 30, Path(sys.executable), _on_marker
    )

    assert rc == 0
    assert ("CVOPS_MLFLOW_RUN_ID", "run-xyz") in seen
    assert ("CVOPS_MLFLOW_EXPERIMENT_ID", "7") in seen
    assert "epoch 1 done" in logs


async def test_run_training_rejects_entry_point_escape(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    import pytest

    with pytest.raises(RuntimeError, match="escapes repo directory"):
        await _run_training(repo, "../evil.py", {}, 30, Path(sys.executable))
