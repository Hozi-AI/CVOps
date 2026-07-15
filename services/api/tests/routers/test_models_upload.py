"""Tests for manual model upload endpoints.

Covers:
  - GET /projects/{id}/models/upload-url  → presigned PUT URL
  - POST /projects/{id}/models             → create ModelVersion (manual upload)
  - PATCH /models/{id}                     → update name/mlflow_run_id
  - 404 on wrong project                   → access control

Pattern mirrors test_models.py: local seed + minimal FastAPI app, no global
fixtures. get_storage is patched to avoid touching real S3.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cvops_api.core.auth import get_current_user
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import Org, User
from cvops_api.db.models.blobs import Blob
from cvops_api.db.models.projects import Project
from cvops_api.db.models.models import ModelVersion
from cvops_api.routers import models


# ---------------------------------------------------------------------------
# Fake storage
# ---------------------------------------------------------------------------


class _FakeStorage:
    async def get_presigned_put(
        self, blob_hash: str, ttl_seconds: int = 3600, endpoint: str | None = None
    ) -> str:
        return f"https://signed.example/put/{blob_hash}"

    async def get_presigned_get(
        self, blob_hash: str, ttl_seconds: int = 900, endpoint: str | None = None
    ) -> str:
        return f"https://signed.example/get/{blob_hash}"


@pytest_asyncio.fixture
async def factory(postgres_url: str):
    engine = create_async_engine(postgres_url, echo=False)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
def patched_storage():
    with patch.object(models, "get_storage", lambda: _FakeStorage()):
        yield


def _client(factory, current_user: User) -> AsyncClient:
    app = FastAPI()
    app.include_router(models.router)

    async def _get_session_dep():
        async with factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_session_dep
    app.dependency_overrides[get_current_user] = lambda: current_user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _hash(suffix: str = "") -> str:
    return "sha256:" + (uuid.uuid4().hex + suffix).ljust(64, "0")[:64]


async def _seed(factory, *, with_mv: bool = False):
    """Create org/user/project + optional blob + optional ModelVersion.

    Returns (user, project, blob, model_version_or_none).
    """
    suffix = uuid.uuid4().hex[:8]
    async with factory() as s:
        org = Org(name=f"org-{suffix}")
        s.add(org)
        await s.flush()
        user = User(org_id=org.id, email=f"u-{suffix}@test.com")
        s.add(user)
        project = Project(org_id=org.id, name=f"proj-{suffix}")
        s.add(project)
        await s.flush()

        blob_hash = _hash()
        blob = Blob(
            hash=blob_hash,
            storage_backend="s3",
            storage_key=f"blobs/{blob_hash[:2]}/{blob_hash[2:]}",
            size_bytes=1024,
            media_type="application/octet-stream",
        )
        s.add(blob)
        await s.flush()

        mv = None
        if with_mv:
            mv = ModelVersion(
                project_id=project.id,
                blob_hash=blob_hash,
                name="initial-name",
                base_model="yolov8n",
            )
            s.add(mv)
            await s.flush()

        await s.commit()
        await s.refresh(user)
        await s.refresh(project)
        await s.refresh(blob)
        if mv:
            await s.refresh(mv)
        return user, project, blob, mv


async def test_get_upload_url_returns_presigned(factory) -> None:
    user, project, _, _ = await _seed(factory)
    blob_hash = _hash()
    async with _client(factory, user) as c:
        res = await c.get(
            f"/projects/{project.id}/models/upload-url",
            params={"blob_hash": blob_hash},
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert "upload_url" in body
    assert blob_hash in body["upload_url"]


async def test_get_upload_url_wrong_project_404(factory) -> None:
    user, _project, _, _ = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.get(
            f"/projects/{uuid.uuid4()}/models/upload-url",
            params={"blob_hash": _hash()},
        )
    assert res.status_code == 404, res.text


async def test_create_model_version_manual(factory) -> None:
    """POST creates a ModelVersion with name/description, no commit required."""
    user, project, blob, _ = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.post(
            f"/projects/{project.id}/models",
            json={
                "blob_hash": blob.hash,
                "size_bytes": 1024,
                "name": "yolov8-nano",
                "description": "Test upload",
                "base_model": "yolov8n",
            },
        )
    assert res.status_code == 201, res.text
    data = res.json()
    assert data["name"] == "yolov8-nano"
    assert data["description"] == "Test upload"
    assert data["project_id"] == str(project.id)


async def test_create_model_version_wrong_project_404(factory) -> None:
    user, _project, blob, _ = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.post(
            f"/projects/{uuid.uuid4()}/models",
            json={"blob_hash": blob.hash, "size_bytes": 1},
        )
    assert res.status_code == 404, res.text


async def test_patch_model_version(factory) -> None:
    user, _project, _, mv = await _seed(factory, with_mv=True)
    async with _client(factory, user) as c:
        res = await c.patch(
            f"/models/{mv.id}",
            json={"name": "updated-name", "mlflow_run_id": "run-abc123"},
        )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["name"] == "updated-name"
    assert data["mlflow_run_id"] == "run-abc123"


async def test_patch_missing_model_404(factory) -> None:
    user, _project, _, _ = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.patch(f"/models/{uuid.uuid4()}", json={"name": "x"})
    assert res.status_code == 404, res.text
