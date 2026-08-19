"""Integration test for the export_yolo step.

Seeds a commit (samples + image blobs in moto S3 + annotation revisions + splits)
and drives ExportYoloStep, then unpacks the produced archive to assert the YOLO
layout, data.yaml class order, and label contents. Also checks the archive is
deterministic (same commit → same export_blob_hash).
"""

from __future__ import annotations

import gzip
import io
import json
import tarfile
import uuid
from unittest.mock import patch

from moto import mock_aws
from PIL import Image
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.config import settings
from cvops_api.core.storage import S3Backend
from cvops_api.engine.step import StepContext
from cvops_steps.export_yolo import ExportYoloStep


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


def _jpeg(color: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(buf, format="JPEG")
    return buf.getvalue()


async def _emit(**kw):
    return None


async def _seed_commit(session: AsyncSession, backend: S3Backend) -> tuple[str, str, list]:
    """Seed project/ontology/dataset + 2 samples in 'train', 1 in 'val'.

    Returns (project_id, commit_id, [(sample_id, split, class_key)]).
    """
    org_id, proj_id, ont_id, ds_id = (uuid.uuid4() for _ in range(4))
    src_id, commit_id = uuid.uuid4(), uuid.uuid4()

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
    for key, order in [("cat", 0), ("dog", 1)]:
        await session.execute(
            text(
                "INSERT INTO label_classes (id, ontology_id, class_key, display_name, "
                "sort_order) VALUES (:i, :o, :k, :k, :s)"
            ),
            {"i": uuid.uuid4(), "o": ont_id, "k": key, "s": order},
        )
    await session.execute(
        text("INSERT INTO datasets (id, project_id, name) VALUES (:i, :p, 'ds')"),
        {"i": ds_id, "p": proj_id},
    )
    await session.execute(
        text(
            "INSERT INTO data_sources (id, project_id, type, status) "
            "VALUES (:i, :p, 'video', 'ingested')"
        ),
        {"i": src_id, "p": proj_id},
    )
    await session.execute(
        text(
            "INSERT INTO commits (id, project_id, dataset_id, ontology_id, "
            "ontology_version, message) VALUES (:i, :p, :d, :o, 1, 'c')"
        ),
        {"i": commit_id, "p": proj_id, "d": ds_id, "o": ont_id},
    )

    layout = [("train", "cat"), ("train", "dog"), ("val", "cat")]
    placed = []
    for idx, (split, class_key) in enumerate(layout):
        sid, rid = uuid.uuid4(), uuid.uuid4()
        blob_hash = await backend.save_bytes(_jpeg((idx * 40, 0, 0)), "image/jpeg")
        await session.execute(
            text(
                "INSERT INTO blobs (hash, storage_backend, storage_key, size_bytes, "
                "media_type) VALUES (:h, 'garage', :k, 10, 'image/jpeg') "
                "ON CONFLICT (hash) DO NOTHING"
            ),
            {"h": blob_hash, "k": f"blobs/{blob_hash[7:9]}"},
        )
        await session.execute(
            text(
                "INSERT INTO samples (id, project_id, blob_hash, source_id, width, "
                "height, frame_index) VALUES (:i, :p, :b, :s, 32, 32, :f)"
            ),
            {"i": sid, "p": proj_id, "b": blob_hash, "s": src_id, "f": idx},
        )
        payload = [
            {
                "class_key": class_key,
                "geometry": {"type": "bbox", "coords": [0.5, 0.5, 0.25, 0.25]},
                "confidence": 0.8,
            }
        ]
        await session.execute(
            text(
                "INSERT INTO annotation_revisions (id, project_id, sample_id, "
                "ontology_id, ontology_version, revision_no, payload, provenance) "
                "VALUES (:i, :p, :s, :o, 1, 1, CAST(:pl AS jsonb), CAST(:pv AS jsonb))"
            ),
            {
                "i": rid, "p": proj_id, "s": sid, "o": ont_id,
                "pl": json.dumps(payload),
                "pv": json.dumps({"source": "model", "review_status": "unreviewed"}),
            },
        )
        await session.execute(
            text(
                "INSERT INTO commit_samples (commit_id, sample_id, "
                "annotation_revision_id, split) VALUES (:c, :s, :r, :sp)"
            ),
            {"c": commit_id, "s": sid, "r": rid, "sp": split},
        )
        placed.append((str(sid), split, class_key))
    await session.flush()
    return str(proj_id), str(commit_id), placed


def _untar(data: bytes) -> dict[str, bytes]:
    raw = gzip.decompress(data)
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r") as tar:
        for m in tar.getmembers():
            f = tar.extractfile(m)
            out[m.name] = f.read() if f else b""
    return out


async def test_export_yolo_produces_valid_archive(session: AsyncSession) -> None:
    s1, s2, s3, s4, s5, s6 = _moto_settings()
    with mock_aws(), s1, s2, s3, s4, s5, s6:
        backend = _mocked_backend()
        proj_id, commit_id, placed = await _seed_commit(session, backend)

        ctx = StepContext(
            session=session, storage=backend, project_id=proj_id,
            run_id=str(uuid.uuid4()), actor_id=str(uuid.uuid4()), emit_event=_emit,
        )
        result = await ExportYoloStep().run(ctx, {}, {"commit_id": commit_id})
        assert result["image_count"] == 3 and result["class_count"] == 2

        members = _untar(await backend.get_bytes(result["export_blob_hash"]))

    # data.yaml: class order follows sort_order (cat=0, dog=1).
    yaml = members["data.yaml"].decode()
    assert "nc: 2" in yaml
    assert '"cat", "dog"' in yaml

    for sid, split, class_key in placed:
        assert f"images/{split}/{sid}.jpg" in members
        label = members[f"labels/{split}/{sid}.txt"].decode().strip()
        expected_idx = 0 if class_key == "cat" else 1
        assert label.startswith(f"{expected_idx} ")
        assert label.split() == [str(expected_idx), "0.500000", "0.500000", "0.250000", "0.250000"]


async def test_export_yolo_is_deterministic(session: AsyncSession) -> None:
    s1, s2, s3, s4, s5, s6 = _moto_settings()
    with mock_aws(), s1, s2, s3, s4, s5, s6:
        backend = _mocked_backend()
        proj_id, commit_id, _ = await _seed_commit(session, backend)
        ctx = StepContext(
            session=session, storage=backend, project_id=proj_id,
            run_id=str(uuid.uuid4()), actor_id=str(uuid.uuid4()), emit_event=_emit,
        )
        step = ExportYoloStep()
        first = await step.run(ctx, {}, {"commit_id": commit_id})
        second = await step.run(ctx, {}, {"commit_id": commit_id})
        # Same commit → byte-identical archive → same content-addressed hash.
        assert first["export_blob_hash"] == second["export_blob_hash"]
