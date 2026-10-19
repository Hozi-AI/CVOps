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

import pytest
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
    await session.execute(
        text("INSERT INTO orgs (id, name) VALUES (:i, :n)"),
        {"i": org_id, "n": f"org-{uuid.uuid4().hex[:6]}"},
    )
    await session.execute(
        text("INSERT INTO projects (id, org_id, name) VALUES (:i, :o, :n)"),
        {"i": proj_id, "o": org_id, "n": "proj"},
    )
    await session.execute(
        text("INSERT INTO ontologies (id, org_id, name, version) VALUES (:i, :o, 'o', 1)"),
        {"i": ont_id, "o": org_id},
    )
    await session.execute(
        text(
            "INSERT INTO label_classes (id, ontology_id, class_key, display_name, sort_order) "
            "VALUES (:i, :o, 'cat', 'Cat', 0)"
        ),
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

    sample = (
        await session.execute(
            text("SELECT id FROM samples WHERE project_id = CAST(:p AS uuid)"),
            {"p": proj_id},
        )
    ).first()
    assert sample is not None

    rev = (
        await session.execute(
            text(
                "SELECT payload, provenance FROM annotation_revisions "
                "WHERE sample_id = CAST(:s AS uuid)"
            ),
            {"s": sample[0]},
        )
    ).first()
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


# ── server-path and zip safety (#185) ──────────────────────────────────────


def _ctx(session: AsyncSession, proj_id: str, storage) -> StepContext:
    return StepContext(
        session=session,
        storage=storage,
        project_id=proj_id,
        run_id=str(uuid.uuid4()),
        actor_id=str(uuid.uuid4()),
        emit_event=_emit,
    )


def _jpg(value: int) -> bytes:
    import cv2
    import numpy as np

    ok, encoded = cv2.imencode(".jpg", np.full((64, 64, 3), value, dtype=np.uint8))
    assert ok
    return encoded.tobytes()


def _write_yolo_folder(folder, value: int = 100) -> None:
    (folder / "images").mkdir(parents=True)
    (folder / "labels").mkdir()
    (folder / "images" / "frame0.jpg").write_bytes(_jpg(value))
    (folder / "labels" / "frame0.txt").write_text("0 0.5 0.5 0.2 0.3\n")
    (folder / "data.yaml").write_text(yaml.dump({"names": ["cat", "dog"]}))


async def test_folder_import_is_disabled_by_default(session: AsyncSession, tmp_path) -> None:
    proj_id, ont_id, _ = await _seed(session)
    _write_yolo_folder(tmp_path / "ds")

    with patch.object(settings, "IMPORT_ROOTS", ""):
        with pytest.raises(ValueError, match="disabled"):
            await ImportDatasetStep().run(
                _ctx(session, proj_id, storage=None),
                config={"format": "auto", "ontology_id": ont_id},
                inputs={"folder_path": str(tmp_path / "ds")},
            )


async def test_folder_outside_allowed_root_is_rejected(session: AsyncSession, tmp_path) -> None:
    proj_id, ont_id, _ = await _seed(session)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    _write_yolo_folder(tmp_path / "elsewhere")

    with patch.object(settings, "IMPORT_ROOTS", str(allowed)):
        with pytest.raises(ValueError, match="outside"):
            await ImportDatasetStep().run(
                _ctx(session, proj_id, storage=None),
                config={"format": "auto", "ontology_id": ont_id},
                inputs={"folder_path": str(tmp_path / "elsewhere")},
            )

    count = (
        await session.execute(
            text("SELECT count(*) FROM samples WHERE project_id = CAST(:p AS uuid)"),
            {"p": proj_id},
        )
    ).scalar_one()
    assert count == 0  # nothing was read or imported


async def test_folder_inside_allowed_root_still_imports(session: AsyncSession, tmp_path) -> None:
    proj_id, ont_id, _ = await _seed(session)
    allowed = tmp_path / "allowed"
    _write_yolo_folder(allowed / "ds")

    s1, s2, s3, s4, s5, s6 = _moto_settings()
    with mock_aws(), s1, s2, s3, s4, s5, s6, patch.object(settings, "IMPORT_ROOTS", str(allowed)):
        import boto3

        boto3.client("s3").create_bucket(Bucket=settings.S3_BUCKET)
        result = await ImportDatasetStep().run(
            _ctx(session, proj_id, S3Backend()),
            config={"format": "auto", "ontology_id": ont_id},
            inputs={"folder_path": str(allowed / "ds")},
        )

    assert len(result["sample_ids"]) == 1
    assert len(result["annotation_revision_ids"]) == 1


async def test_symlinked_image_cannot_escape_the_folder(session: AsyncSession, tmp_path) -> None:
    proj_id, ont_id, _ = await _seed(session)
    allowed = tmp_path / "allowed"
    _write_yolo_folder(allowed / "ds", value=100)
    # An image outside the allowed root, linked from inside the dataset folder.
    outside = tmp_path / "private.jpg"
    outside.write_bytes(_jpg(200))
    (allowed / "ds" / "images" / "stolen.jpg").symlink_to(outside)

    s1, s2, s3, s4, s5, s6 = _moto_settings()
    with mock_aws(), s1, s2, s3, s4, s5, s6, patch.object(settings, "IMPORT_ROOTS", str(allowed)):
        import boto3

        boto3.client("s3").create_bucket(Bucket=settings.S3_BUCKET)
        result = await ImportDatasetStep().run(
            _ctx(session, proj_id, S3Backend()),
            config={"format": "auto", "ontology_id": ont_id},
            inputs={"folder_path": str(allowed / "ds")},
        )

    assert len(result["sample_ids"]) == 1  # only the real image, not the symlink


async def test_zip_over_entry_limit_is_rejected(session: AsyncSession) -> None:
    proj_id, ont_id, _ = await _seed(session)
    zip_data, zip_hash = _make_yolo_zip()  # 3 entries

    s1, s2, s3, s4, s5, s6 = _moto_settings()
    with mock_aws(), s1, s2, s3, s4, s5, s6, patch.object(settings, "IMPORT_MAX_ZIP_ENTRIES", 2):
        import boto3

        boto3.client("s3").create_bucket(Bucket=settings.S3_BUCKET)
        backend = S3Backend()
        await backend.save_bytes(zip_data, "application/zip")
        with pytest.raises(ValueError, match="too many entries"):
            await ImportDatasetStep().run(
                _ctx(session, proj_id, backend),
                config={"format": "auto", "ontology_id": ont_id},
                inputs={"blob_hash": zip_hash},
            )

    count = (
        await session.execute(
            text("SELECT count(*) FROM samples WHERE project_id = CAST(:p AS uuid)"),
            {"p": proj_id},
        )
    ).scalar_one()
    assert count == 0
