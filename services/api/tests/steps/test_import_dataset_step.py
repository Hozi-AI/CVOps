"""Integration test for ImportDatasetStep.run().

Seeds org/project/ontology in testcontainers Postgres. Builds a synthetic YOLO
dataset zip in memory, stores it in moto S3, then runs the step end-to-end.
Asserts samples and annotation_revisions rows are created correctly.
"""
from __future__ import annotations

import hashlib
import io
import json
import uuid
import zipfile
from unittest.mock import patch

import yaml
from moto import mock_aws
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.config import settings
from cvops_api.core.storage import S3Backend
from cvops_api.engine.step import StepContext
from cvops_steps.import_dataset import ImportDatasetStep


def _moto_settings():
    return (
        patch.object(settings, "S3_ENDPOINT", None),
        patch.object(settings, "S3_REGION", "us-east-1"),
        patch.object(settings, "S3_ACCESS_KEY", "testing"),
        patch.object(settings, "S3_SECRET_KEY", "testing"),
        patch.object(settings, "S3_BUCKET", "test-bucket"),
        patch.object(settings, "S3_PUBLIC_ENDPOINT", ""),
    )


def _make_yolo_zip() -> tuple[bytes, str]:
    """Build a minimal YOLO zip: 1 image + 1 label + data.yaml. Returns (bytes, sha256)."""
    import cv2
    import numpy as np

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        img = np.zeros((64, 64, 3), dtype=np.uint8)
        img[:, :] = [100, 150, 200]
        ok, encoded = cv2.imencode(".jpg", img)
        assert ok
        zf.writestr("images/frame0.jpg", encoded.tobytes())
        zf.writestr("labels/frame0.txt", "0 0.5 0.5 0.2 0.3\n")
        zf.writestr("data.yaml", yaml.dump({"names": ["cat", "dog"]}))
    data = buf.getvalue()
    sha = "sha256:" + hashlib.sha256(data).hexdigest()
    return data, sha


async def _seed(session: AsyncSession) -> tuple[str, str, str]:
    """Create org/project/ontology with 'cat' label class. Returns (project_id, ontology_id, cls_id)."""
    org_id, proj_id, ont_id, cls_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await session.execute(text("INSERT INTO orgs (id, name) VALUES (:i, :n)"),
                          {"i": org_id, "n": f"org-{uuid.uuid4().hex[:6]}"})
    await session.execute(text("INSERT INTO projects (id, org_id, name) VALUES (:i, :o, :n)"),
                          {"i": proj_id, "o": org_id, "n": "proj"})
    await session.execute(text("INSERT INTO ontologies (id, org_id, name, version) VALUES (:i, :o, 'o', 1)"),
                          {"i": ont_id, "o": org_id})
    await session.execute(
        text("INSERT INTO label_classes (id, ontology_id, class_key, display_name, sort_order) "
             "VALUES (:i, :o, 'cat', 'Cat', 0)"),
        {"i": cls_id, "o": ont_id},
    )
    await session.flush()
    return str(proj_id), str(ont_id), str(cls_id)


async def _emit(**kw) -> None:
    pass


async def test_import_yolo_zip_creates_sample_and_revision(session: AsyncSession) -> None:
    proj_id, ont_id, _cls_id = await _seed(session)
    zip_data, zip_hash = _make_yolo_zip()

    s1, s2, s3, s4, s5, s6 = _moto_settings()
    with mock_aws(), s1, s2, s3, s4, s5, s6:
        import boto3
        boto3.client("s3").create_bucket(Bucket=settings.S3_BUCKET)
        backend = S3Backend()
        stored_hash = await backend.save_bytes(zip_data, "application/zip")
        assert stored_hash == zip_hash

        ctx = StepContext(
            session=session,
            storage=backend,
            project_id=proj_id,
            run_id=str(uuid.uuid4()),
            actor_id=str(uuid.uuid4()),
            emit_event=_emit,
        )
        result = await ImportDatasetStep().run(
            ctx,
            config={"format": "auto", "ontology_id": ont_id},
            inputs={"blob_hash": zip_hash},
        )

    assert len(result["sample_ids"]) == 1
    assert len(result["annotation_revision_ids"]) == 1

    sample = (await session.execute(
        text("SELECT id FROM samples WHERE project_id = CAST(:p AS uuid)"),
        {"p": proj_id},
    )).first()
    assert sample is not None

    rev = (await session.execute(
        text("SELECT payload, provenance FROM annotation_revisions "
             "WHERE sample_id = CAST(:s AS uuid)"),
        {"s": sample[0]},
    )).first()
    assert rev is not None
    payload = rev[0] if isinstance(rev[0], list) else json.loads(rev[0])
    assert payload[0]["class_key"] == "cat"
    provenance = rev[1] if isinstance(rev[1], dict) else json.loads(rev[1])
    assert provenance["source"] == "import"


async def test_import_no_ontology_skips_revisions(session: AsyncSession) -> None:
    proj_id, _ont_id, _cls_id = await _seed(session)
    zip_data, zip_hash = _make_yolo_zip()

    s1, s2, s3, s4, s5, s6 = _moto_settings()
    with mock_aws(), s1, s2, s3, s4, s5, s6:
        import boto3
        boto3.client("s3").create_bucket(Bucket=settings.S3_BUCKET)
        backend = S3Backend()
        await backend.save_bytes(zip_data, "application/zip")

        ctx = StepContext(
            session=session,
            storage=backend,
            project_id=proj_id,
            run_id=str(uuid.uuid4()),
            actor_id=str(uuid.uuid4()),
            emit_event=_emit,
        )
        result = await ImportDatasetStep().run(
            ctx,
            config={"format": "auto"},
            inputs={"blob_hash": zip_hash},
        )

    assert len(result["sample_ids"]) == 1
    assert result["annotation_revision_ids"] == []
