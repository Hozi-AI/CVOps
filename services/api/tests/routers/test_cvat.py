"""Router tests for the CVAT / model-deployer surface (``routers/cvat.py``).

Covers the three endpoints, all behind ``get_current_user``:

- ``POST /models/{id}/cvat-deploy`` — fetch presigned weights, then POST to the
  deployer ``/deploy``. 502 on weights-fetch failure or deployer error, 404 on
  unknown model.
- ``GET /cvat/models`` — proxy the deployer ``/models``; 502 on non-200.
- ``POST /projects/{id}/cvat-annotate`` — multipart upload proxied to the
  deployer ``/annotate``; 502 on non-200.

The deployer HTTP calls (and the presigned weights GET) are mocked with respx.
``get_storage`` is faked to return a stable presigned URL that respx also mocks.
The cvat router reads ``DEPLOYER_URL`` from settings at import time, so tests
reference ``cvat.DEPLOYER_URL`` rather than re-reading settings.

ModelVersion has three NOT NULL foreign keys (blob_hash, trained_on_commit_id,
training_container_id), so each row is seeded inline with a Blob, Dataset,
Commit, and TrainingContainer — mirroring test_models.py.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import httpx
import pytest_asyncio
import respx
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cvops_api.core.auth import get_current_user
from cvops_api.db.models.auth import Org, User
from cvops_api.db.models.blobs import Blob
from cvops_api.db.models.models import ModelVersion, TrainingContainer
from cvops_api.db.models.projects import Project
from cvops_api.db.models.versioning import Commit, Dataset
from cvops_api.db.session import get_session
from cvops_api.routers import cvat

WEIGHTS_URL = "https://signed.example/weights.pt"


# ---------------------------------------------------------------------------
# Fake storage
# ---------------------------------------------------------------------------


class _FakeStorage:
    async def get_presigned_get(
        self, blob_hash: str, ttl_seconds: int = 900, endpoint: str | None = None
    ) -> str:
        return WEIGHTS_URL


@pytest_asyncio.fixture
async def factory(postgres_url: str):
    engine = create_async_engine(postgres_url, echo=False)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
def patched_storage():
    with patch.object(cvat, "get_storage", lambda: _FakeStorage()):
        yield


def _client(factory, current_user: User) -> AsyncClient:
    app = FastAPI()
    app.include_router(cvat.router)

    async def _get_session_dep():
        async with factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_session_dep
    app.dependency_overrides[get_current_user] = lambda: current_user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _hash() -> str:
    return "sha256:" + uuid.uuid4().hex.ljust(64, "0")


async def _seed(factory):
    """Create org/user/project plus the FK chain a ModelVersion needs.

    Returns (user, project, model_version_id).
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
        return user, project, mv.id


# ---------------------------------------------------------------------------
# POST /models/{id}/cvat-deploy
# ---------------------------------------------------------------------------


async def test_cvat_deploy_happy_path(factory) -> None:
    user, _project, mv_id = await _seed(factory)
    with respx.mock(assert_all_called=True) as mock:
        mock.get(WEIGHTS_URL).mock(return_value=httpx.Response(200, content=b"weights"))
        mock.post(f"{cvat.DEPLOYER_URL}/deploy").mock(
            return_value=httpx.Response(200, json={"status": "deployed", "function": "yolo"})
        )
        async with _client(factory, user) as c:
            res = await c.post(f"/models/{mv_id}/cvat-deploy?model_name=my-model")
    assert res.status_code == 200, res.text
    assert res.json() == {"status": "deployed", "function": "yolo"}


async def test_cvat_deploy_weights_fetch_non_200(factory) -> None:
    user, _project, mv_id = await _seed(factory)
    with respx.mock as mock:
        mock.get(WEIGHTS_URL).mock(return_value=httpx.Response(404, text="gone"))
        async with _client(factory, user) as c:
            res = await c.post(f"/models/{mv_id}/cvat-deploy?model_name=my-model")
    assert res.status_code == 502, res.text
    assert "weights" in res.json()["detail"].lower()


async def test_cvat_deploy_deployer_error_502(factory) -> None:
    user, _project, mv_id = await _seed(factory)
    with respx.mock as mock:
        mock.get(WEIGHTS_URL).mock(return_value=httpx.Response(200, content=b"weights"))
        mock.post(f"{cvat.DEPLOYER_URL}/deploy").mock(
            return_value=httpx.Response(500, text="boom on deployer")
        )
        async with _client(factory, user) as c:
            res = await c.post(f"/models/{mv_id}/cvat-deploy?model_name=my-model")
    assert res.status_code == 502, res.text
    assert "boom on deployer" in res.json()["detail"]


async def test_cvat_deploy_unknown_model_404(factory) -> None:
    user, _project, _mv_id = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.post(f"/models/{uuid.uuid4()}/cvat-deploy?model_name=my-model")
    assert res.status_code == 404, res.text


async def test_cvat_deploy_cross_org(factory) -> None:
    # BUG: _get_model_version lacks org scoping — it queries ModelVersion by id
    # only, with no org_id filter. A user in org B can deploy a model owned by
    # org A. Pinning current (leaky) behavior; documents the multi-tenancy gap
    # from docs/GAP_ANALYSIS.md.
    _owner, _project, mv_id = await _seed(factory)
    other, _p2, _mv2 = await _seed(factory)
    with respx.mock as mock:
        mock.get(WEIGHTS_URL).mock(return_value=httpx.Response(200, content=b"weights"))
        mock.post(f"{cvat.DEPLOYER_URL}/deploy").mock(
            return_value=httpx.Response(200, json={"status": "deployed"})
        )
        async with _client(factory, other) as c:
            res = await c.post(f"/models/{mv_id}/cvat-deploy?model_name=my-model")
    # Leaks: cross-org model is reachable and deploys successfully (200, not 404).
    assert res.status_code == 200, res.text
    assert res.json() == {"status": "deployed"}


# ---------------------------------------------------------------------------
# GET /cvat/models
# ---------------------------------------------------------------------------


async def test_list_cvat_models_happy(factory) -> None:
    user, _project, _mv_id = await _seed(factory)
    payload = [{"id": "yolo", "name": "YOLOv8"}, {"id": "sam", "name": "SAM"}]
    with respx.mock as mock:
        mock.get(f"{cvat.DEPLOYER_URL}/models").mock(return_value=httpx.Response(200, json=payload))
        async with _client(factory, user) as c:
            res = await c.get("/cvat/models")
    assert res.status_code == 200, res.text
    assert res.json() == payload


async def test_list_cvat_models_deployer_error_502(factory) -> None:
    user, _project, _mv_id = await _seed(factory)
    with respx.mock as mock:
        mock.get(f"{cvat.DEPLOYER_URL}/models").mock(
            return_value=httpx.Response(500, text="deployer down")
        )
        async with _client(factory, user) as c:
            res = await c.get("/cvat/models")
    assert res.status_code == 502, res.text
    assert "deployer down" in res.json()["detail"]


# ---------------------------------------------------------------------------
# POST /projects/{project_id}/cvat-annotate
# ---------------------------------------------------------------------------


async def test_cvat_annotate_accepts_form_params(factory) -> None:
    """cvat_annotate should accept task_name/function_id as form fields, not JSON body."""
    user, project, _mv_id = await _seed(factory)
    with respx.mock as mock:
        # Mock the deployer response
        mock.post(f"{cvat.DEPLOYER_URL}/annotate").mock(
            return_value=httpx.Response(200, json={"task_id": 1, "job_id": 2, "cvat_url": "http://x"})
        )
        async with _client(factory, user) as c:
            res = await c.post(
                f"/projects/{project.id}/cvat-annotate",
                data={"task_name": "my-task", "function_id": "yolo-detector", "threshold": "0.3"},
                files={"files": ("a.jpg", b"\xff\xd8\xff fake", "image/jpeg")},
            )
    assert res.status_code == 200, res.text
    assert res.json() == {"task_id": 1, "job_id": 2, "cvat_url": "http://x"}
