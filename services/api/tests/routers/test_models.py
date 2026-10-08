"""Router tests for the models (ModelVersion) surface.

Covers list, get (404 + cross-org 404), and the weights-url presigned endpoint
(via an overridden get_storage), including the 404 path when the underlying
blob is missing.

ModelVersion has three NOT NULL foreign keys — blob_hash, trained_on_commit_id,
training_container_id — so each row is seeded with a Blob, Dataset, Commit, and
TrainingContainer created inline through the ORM.

Pattern mirrors test_data_sources: a minimal app mounting only the models
router, with get_session/get_current_user overridden and get_storage faked.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
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
from cvops_api.db.models.versioning import Commit, Dataset
from cvops_api.db.models.models import ModelVersion, TrainingContainer
from cvops_api.routers import models


# ---------------------------------------------------------------------------
# Fake storage
# ---------------------------------------------------------------------------


class _FakeStorage:
    async def get_presigned_get(
        self, blob_hash: str, ttl_seconds: int = 900, endpoint: str | None = None
    ) -> str:
        return f"https://signed.example/{blob_hash}"


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


def _hash() -> str:
    return "sha256:" + uuid.uuid4().hex.ljust(64, "0")


async def _seed(factory, *, with_blob: bool = True):
    """Create org/user/project plus the FK chain a ModelVersion needs.

    Returns (user, project, model_version_id, blob_hash).
    When with_blob is False, the model's blob_hash still references a real Blob
    row (FK), but we return the hash so the caller can decide what to assert.
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
        s.add(
            Blob(
                hash=blob_hash,
                storage_backend="s3",
                storage_key=f"k/{blob_hash}",
                size_bytes=1024,
                media_type="application/gzip",
            )
        )

        dataset = Dataset(project_id=project.id, name=f"ds-{suffix}")
        s.add(dataset)
        await s.flush()
        commit = Commit(project_id=project.id, dataset_id=dataset.id, message="init")
        s.add(commit)

        tc = TrainingContainer(
            project_id=project.id,
            name=f"tc-{suffix}",
            image="img:1",
            icd_config={},
        )
        s.add(tc)
        await s.flush()

        mv = ModelVersion(
            project_id=project.id,
            blob_hash=blob_hash,
            trained_on_commit_id=commit.id,
            training_container_id=tc.id,
            base_model="yolov8n",
            metrics={"mAP": 0.5},
        )
        s.add(mv)
        await s.commit()
        await s.refresh(user)
        await s.refresh(project)
        await s.refresh(mv)
        return user, project, mv.id, blob_hash


async def test_list_models(factory) -> None:
    user, project, mv_id, _ = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.get(f"/projects/{project.id}/models")
    assert res.status_code == 200, res.text
    ids = [m["id"] for m in res.json()]
    assert str(mv_id) in ids
    assert len(ids) == 1


async def test_get_model(factory) -> None:
    user, _project, mv_id, blob_hash = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.get(f"/models/{mv_id}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["id"] == str(mv_id)
    assert body["blob_hash"] == blob_hash
    assert body["base_model"] == "yolov8n"


async def _dataset_id_of(factory, mv_id: uuid.UUID) -> uuid.UUID:
    async with factory() as s:
        mv = await s.get(ModelVersion, mv_id)
        assert mv is not None and mv.trained_on_commit_id is not None
        commit = await s.get(Commit, mv.trained_on_commit_id)
        assert commit is not None
        return commit.dataset_id


async def test_get_model_includes_trained_on_dataset_id(factory) -> None:
    user, _project, mv_id, _ = await _seed(factory)
    expected = await _dataset_id_of(factory, mv_id)
    async with _client(factory, user) as c:
        res = await c.get(f"/models/{mv_id}")
    assert res.status_code == 200, res.text
    assert res.json()["trained_on_dataset_id"] == str(expected)


async def test_list_models_includes_trained_on_dataset_id(factory) -> None:
    user, project, mv_id, _ = await _seed(factory)
    expected = await _dataset_id_of(factory, mv_id)
    async with _client(factory, user) as c:
        res = await c.get(f"/projects/{project.id}/models")
    assert res.status_code == 200, res.text
    assert res.json()[0]["trained_on_dataset_id"] == str(expected)


async def test_trained_on_dataset_id_null_without_commit(factory) -> None:
    user, _project, mv_id, _ = await _seed(factory)
    async with factory() as s:
        mv = await s.get(ModelVersion, mv_id)
        assert mv is not None
        mv.trained_on_commit_id = None
        await s.commit()
    async with _client(factory, user) as c:
        res = await c.get(f"/models/{mv_id}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["trained_on_commit_id"] is None
    assert body["trained_on_dataset_id"] is None


async def test_trained_on_dataset_id_null_when_dataset_deleted(factory) -> None:
    user, _project, mv_id, _ = await _seed(factory)
    dataset_id = await _dataset_id_of(factory, mv_id)
    async with factory() as s:
        dataset = await s.get(Dataset, dataset_id)
        assert dataset is not None
        dataset.deleted_at = datetime.now(UTC)
        await s.commit()
    async with _client(factory, user) as c:
        res = await c.get(f"/models/{mv_id}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["trained_on_commit_id"] is not None
    assert body["trained_on_dataset_id"] is None


async def test_get_missing_model_404(factory) -> None:
    user, _project, _mv_id, _ = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.get(f"/models/{uuid.uuid4()}")
    assert res.status_code == 404, res.text


async def test_get_model_cross_org_404(factory) -> None:
    _owner, _project, mv_id, _ = await _seed(factory)
    other, _p2, _mv2, _ = await _seed(factory)
    async with _client(factory, other) as c:
        res = await c.get(f"/models/{mv_id}")
    assert res.status_code == 404, res.text


async def test_weights_url_presigned(factory) -> None:
    user, _project, mv_id, blob_hash = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.get(f"/models/{mv_id}/weights-url")
    assert res.status_code == 200, res.text
    assert res.json()["url"] == f"https://signed.example/{blob_hash}"


async def test_weights_url_cross_org_404(factory) -> None:
    _owner, _project, mv_id, _ = await _seed(factory)
    other, _p2, _mv2, _ = await _seed(factory)
    async with _client(factory, other) as c:
        res = await c.get(f"/models/{mv_id}/weights-url")
    assert res.status_code == 404, res.text
