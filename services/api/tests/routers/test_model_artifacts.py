"""Tests for model artifact upload and listing.

Covers:
  - GET /models/{id}/artifacts/upload-url  → presigned PUT URL
  - POST /models/{id}/artifacts             → create ModelArtifact
  - GET  /models/{id}/artifacts             → list ModelArtifacts

Pattern mirrors test_models_upload.py: local seed + minimal FastAPI app,
get_storage patched to avoid real S3.
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


def _hash(tag: str = "") -> str:
    return "sha256:" + (uuid.uuid4().hex + tag).ljust(64, "0")[:64]


async def _seed(factory):
    """Create org/user/project/blob/model_version.

    Returns (user, model_version, stored_blob).
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

        blob_hash = _hash("weights")
        weights_blob = Blob(
            hash=blob_hash,
            storage_backend="s3",
            storage_key=f"blobs/{blob_hash[:2]}/{blob_hash[2:]}",
            size_bytes=4096,
            media_type="application/gzip",
        )
        s.add(weights_blob)

        mv = ModelVersion(
            project_id=project.id,
            blob_hash=blob_hash,
            base_model="yolov8n",
        )
        s.add(mv)
        await s.flush()

        # A pre-existing blob to attach as an artifact
        art_hash = _hash("artifact")
        art_blob = Blob(
            hash=art_hash,
            storage_backend="s3",
            storage_key=f"blobs/{art_hash[:2]}/{art_hash[2:]}",
            size_bytes=2048,
            media_type="image/png",
        )
        s.add(art_blob)
        await s.commit()
        await s.refresh(user)
        await s.refresh(mv)
        await s.refresh(art_blob)
        return user, mv, art_blob


async def test_get_artifact_upload_url(factory) -> None:
    user, mv, _ = await _seed(factory)
    blob_hash = _hash("new")
    async with _client(factory, user) as c:
        res = await c.get(
            f"/models/{mv.id}/artifacts/upload-url",
            params={"blob_hash": blob_hash, "filename": "results.png"},
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert "upload_url" in body
    assert blob_hash in body["upload_url"]


async def test_get_artifact_upload_url_wrong_model_404(factory) -> None:
    user, _mv, _ = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.get(
            f"/models/{uuid.uuid4()}/artifacts/upload-url",
            params={"blob_hash": _hash(), "filename": "x.png"},
        )
    assert res.status_code == 404, res.text


async def test_create_and_list_artifacts(factory) -> None:
    user, mv, art_blob = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.post(
            f"/models/{mv.id}/artifacts",
            json={
                "blob_hash": art_blob.hash,
                "filename": "results.png",
                "mime_type": "image/png",
                "size_bytes": 2048,
            },
        )
    assert res.status_code == 201, res.text
    artifact = res.json()
    assert artifact["filename"] == "results.png"
    assert artifact["mime_type"] == "image/png"
    assert artifact["model_version_id"] == str(mv.id)
    assert "url" in artifact
    assert art_blob.hash in artifact["url"]

    # List
    async with _client(factory, user) as c:
        res2 = await c.get(f"/models/{mv.id}/artifacts")
    assert res2.status_code == 200, res2.text
    items = res2.json()
    assert any(a["id"] == artifact["id"] for a in items)


async def test_create_artifact_wrong_model_404(factory) -> None:
    user, _mv, art_blob = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.post(
            f"/models/{uuid.uuid4()}/artifacts",
            json={"blob_hash": art_blob.hash, "filename": "x.png", "size_bytes": 1},
        )
    assert res.status_code == 404, res.text


async def test_list_artifacts_cross_org_404(factory) -> None:
    _owner, mv, _ = await _seed(factory)
    other, _mv2, _ = await _seed(factory)
    async with _client(factory, other) as c:
        res = await c.get(f"/models/{mv.id}/artifacts")
    assert res.status_code == 404, res.text
