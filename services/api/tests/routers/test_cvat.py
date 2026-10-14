"""Router tests for the CVAT / model-deployer surface (``routers/cvat.py``).

Covers the CVAT endpoints, all behind ``get_current_user`` and scoped to the
caller's org (ownership of deployed functions lives in ``cvat_deployments``):

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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cvops_api.core.auth import get_current_user
from cvops_api.db.models.auth import Org, User
from cvops_api.db.models.blobs import Blob
from cvops_api.db.models.models import CvatDeployment, ModelVersion, TrainingContainer
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


def _model_name() -> str:
    """A unique deploy name: the test DB is shared, and function names are global."""
    return f"m-{uuid.uuid4().hex[:8]}"


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


async def _own(factory, user: User, function_name: str, mv_id: uuid.UUID | None = None) -> None:
    """Record ``user``'s org as an owner of a deployed CVAT function."""
    async with factory() as s:
        s.add(
            CvatDeployment(
                org_id=user.org_id,
                function_name=function_name,
                model_name=function_name,
                model_version_id=mv_id,
            )
        )
        await s.commit()


async def _owned(factory, user: User) -> list[CvatDeployment]:
    """Active deployments owned by ``user``'s org."""
    async with factory() as s:
        r = await s.execute(
            select(CvatDeployment).where(
                CvatDeployment.org_id == user.org_id,
                CvatDeployment.deleted_at.is_(None),
            )
        )
        return list(r.scalars().all())


# ---------------------------------------------------------------------------
# POST /models/{id}/cvat-deploy
# ---------------------------------------------------------------------------


async def test_cvat_deploy_happy_path(factory) -> None:
    name = _model_name()
    user, _project, mv_id = await _seed(factory)
    with respx.mock(assert_all_called=True) as mock:
        mock.get(WEIGHTS_URL).mock(return_value=httpx.Response(200, content=b"weights"))
        mock.post(f"{cvat.DEPLOYER_URL}/deploy").mock(
            return_value=httpx.Response(
                200, json={"status": "deployed", "function_name": f"pth-custom-{name}"}
            )
        )
        async with _client(factory, user) as c:
            res = await c.post(f"/models/{mv_id}/cvat-deploy?model_name={name}")
    assert res.status_code == 200, res.text
    assert res.json() == {"status": "deployed", "function_name": f"pth-custom-{name}"}
    # The caller's org now owns the function, linked to the model it came from.
    owned = await _owned(factory, user)
    assert [(d.function_name, d.model_version_id) for d in owned] == [(f"pth-custom-{name}", mv_id)]


async def test_cvat_deploy_name_owned_by_other_org_409(factory) -> None:
    name = _model_name()
    owner, _p, _mv = await _seed(factory)
    await _own(factory, owner, f"pth-custom-{name}")
    user, _project, mv_id = await _seed(factory)
    with respx.mock:  # no routes: the deployer must not be reached
        async with _client(factory, user) as c:
            res = await c.post(f"/models/{mv_id}/cvat-deploy?model_name={name}")
    assert res.status_code == 409, res.text
    assert await _owned(factory, user) == []


async def test_cvat_deploy_again_keeps_single_ownership_row(factory) -> None:
    name = _model_name()
    user, _project, mv_id = await _seed(factory)
    with respx.mock as mock:
        mock.get(WEIGHTS_URL).mock(return_value=httpx.Response(200, content=b"weights"))
        mock.post(f"{cvat.DEPLOYER_URL}/deploy").mock(
            return_value=httpx.Response(200, json={"function_name": f"pth-custom-{name}"})
        )
        async with _client(factory, user) as c:
            first = await c.post(f"/models/{mv_id}/cvat-deploy?model_name={name}")
            second = await c.post(f"/models/{mv_id}/cvat-deploy?model_name={name}")
    assert first.status_code == second.status_code == 200
    assert len(await _owned(factory, user)) == 1


async def test_cvat_deploy_weights_fetch_non_200(factory) -> None:
    name = _model_name()
    user, _project, mv_id = await _seed(factory)
    with respx.mock as mock:
        mock.get(WEIGHTS_URL).mock(return_value=httpx.Response(404, text="gone"))
        async with _client(factory, user) as c:
            res = await c.post(f"/models/{mv_id}/cvat-deploy?model_name={name}")
    assert res.status_code == 502, res.text
    assert "weights" in res.json()["detail"].lower()


async def test_cvat_deploy_deployer_error_502(factory) -> None:
    name = _model_name()
    user, _project, mv_id = await _seed(factory)
    with respx.mock as mock:
        mock.get(WEIGHTS_URL).mock(return_value=httpx.Response(200, content=b"weights"))
        mock.post(f"{cvat.DEPLOYER_URL}/deploy").mock(
            return_value=httpx.Response(500, text="boom on deployer")
        )
        async with _client(factory, user) as c:
            res = await c.post(f"/models/{mv_id}/cvat-deploy?model_name={name}")
    assert res.status_code == 502, res.text
    assert "boom on deployer" in res.json()["detail"]


async def test_cvat_deploy_unknown_model_404(factory) -> None:
    name = _model_name()
    user, _project, _mv_id = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.post(f"/models/{uuid.uuid4()}/cvat-deploy?model_name={name}")
    assert res.status_code == 404, res.text


async def test_cvat_deploy_cross_org(factory) -> None:
    name = _model_name()
    # A user in another org must not be able to deploy a model they don't own:
    # the lookup is scoped by the model's project org, so it reads as not found
    # and nothing is fetched from storage or sent to the deployer.
    _owner, _project, mv_id = await _seed(factory)
    other, _p2, _mv2 = await _seed(factory)
    with respx.mock:  # no routes: any outbound call would raise
        async with _client(factory, other) as c:
            res = await c.post(f"/models/{mv_id}/cvat-deploy?model_name={name}")
    assert res.status_code == 404, res.text
    assert await _owned(factory, other) == []


# ---------------------------------------------------------------------------
# POST /cvat/deploy (raw .pt upload)
# ---------------------------------------------------------------------------


async def test_cvat_deploy_file_records_ownership(factory) -> None:
    name = _model_name()
    user, _project, _mv_id = await _seed(factory)
    with respx.mock as mock:
        mock.post(f"{cvat.DEPLOYER_URL}/deploy").mock(
            return_value=httpx.Response(200, json={"function_name": f"pth-custom-{name}"})
        )
        async with _client(factory, user) as c:
            res = await c.post(
                f"/cvat/deploy?model_name={name}",
                files={"file": ("m.pt", b"weights", "application/octet-stream")},
            )
    assert res.status_code == 200, res.text
    owned = await _owned(factory, user)
    assert [(d.function_name, d.model_version_id) for d in owned] == [(f"pth-custom-{name}", None)]


async def test_cvat_deploy_file_name_owned_by_other_org_409(factory) -> None:
    name = _model_name()
    owner, _p, _mv = await _seed(factory)
    await _own(factory, owner, f"pth-custom-{name}")
    user, _project, _mv_id = await _seed(factory)
    with respx.mock:  # no routes: the deployer must not be reached
        async with _client(factory, user) as c:
            res = await c.post(
                f"/cvat/deploy?model_name={name}",
                files={"file": ("m.pt", b"weights", "application/octet-stream")},
            )
    assert res.status_code == 409, res.text


# ---------------------------------------------------------------------------
# GET /cvat/models
# ---------------------------------------------------------------------------


async def test_list_cvat_models_happy(factory) -> None:
    user, _project, _mv_id = await _seed(factory)
    other, _p2, _mv2 = await _seed(factory)
    await _own(factory, user, "yolo")
    await _own(factory, other, "sam")
    payload = [{"id": "yolo", "name": "YOLOv8"}, {"id": "sam", "name": "SAM"}]
    with respx.mock as mock:
        mock.get(f"{cvat.DEPLOYER_URL}/models").mock(return_value=httpx.Response(200, json=payload))
        async with _client(factory, user) as c:
            res = await c.get("/cvat/models")
    assert res.status_code == 200, res.text
    # Only the caller's org's function: the other org's "sam" is hidden.
    assert res.json() == [{"id": "yolo", "name": "YOLOv8"}]


async def test_list_cvat_models_unowned_functions_hidden(factory) -> None:
    user, _project, _mv_id = await _seed(factory)
    with respx.mock as mock:
        mock.get(f"{cvat.DEPLOYER_URL}/models").mock(
            return_value=httpx.Response(200, json=[{"id": "legacy", "name": "Legacy"}])
        )
        async with _client(factory, user) as c:
            res = await c.get("/cvat/models")
    assert res.status_code == 200, res.text
    assert res.json() == []


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
    await _own(factory, user, "yolo-detector")
    with respx.mock as mock:
        # Mock the deployer response
        mock.post(f"{cvat.DEPLOYER_URL}/annotate").mock(
            return_value=httpx.Response(
                200, json={"task_id": 1, "job_id": 2, "cvat_url": "http://x"}
            )
        )
        async with _client(factory, user) as c:
            res = await c.post(
                f"/projects/{project.id}/cvat-annotate",
                data={"task_name": "my-task", "function_id": "yolo-detector", "threshold": "0.3"},
                files={"files": ("a.jpg", b"\xff\xd8\xff fake", "image/jpeg")},
            )
    assert res.status_code == 200, res.text
    assert res.json() == {"task_id": 1, "job_id": 2, "cvat_url": "http://x"}


async def test_cvat_annotate_foreign_project_404(factory) -> None:
    user, _project, _mv_id = await _seed(factory)
    await _own(factory, user, "yolo-detector")
    _other, other_project, _mv2 = await _seed(factory)
    with respx.mock:  # no routes: the deployer must not be reached
        async with _client(factory, user) as c:
            res = await c.post(
                f"/projects/{other_project.id}/cvat-annotate",
                data={"task_name": "t", "function_id": "yolo-detector"},
                files={"files": ("a.jpg", b"\xff\xd8\xff fake", "image/jpeg")},
            )
    assert res.status_code == 404, res.text


async def test_cvat_annotate_foreign_function_404(factory) -> None:
    user, project, _mv_id = await _seed(factory)
    other, _p2, _mv2 = await _seed(factory)
    await _own(factory, other, "their-model")
    with respx.mock:  # no routes: the deployer must not be reached
        async with _client(factory, user) as c:
            res = await c.post(
                f"/projects/{project.id}/cvat-annotate",
                data={"task_name": "t", "function_id": "their-model"},
                files={"files": ("a.jpg", b"\xff\xd8\xff fake", "image/jpeg")},
            )
    assert res.status_code == 404, res.text


# ---------------------------------------------------------------------------
# DELETE /cvat/models/{function_id}
# ---------------------------------------------------------------------------


async def test_cvat_delete_not_owned_404(factory) -> None:
    fn = _model_name()
    user, _project, _mv_id = await _seed(factory)
    other, _p2, _mv2 = await _seed(factory)
    await _own(factory, other, fn)
    with respx.mock:  # no routes: the deployer must not be reached
        async with _client(factory, user) as c:
            res = await c.delete(f"/cvat/models/{fn}")
    assert res.status_code == 404, res.text
    assert len(await _owned(factory, other)) == 1


async def test_cvat_delete_last_owner_removes_function(factory) -> None:
    fn = _model_name()
    user, _project, _mv_id = await _seed(factory)
    await _own(factory, user, fn)
    with respx.mock(assert_all_called=True) as mock:
        mock.delete(f"{cvat.DEPLOYER_URL}/models/{fn}").mock(
            return_value=httpx.Response(200, json={"deleted": fn})
        )
        async with _client(factory, user) as c:
            res = await c.delete(f"/cvat/models/{fn}")
    assert res.status_code == 200, res.text
    assert res.json() == {"deleted": fn}
    assert await _owned(factory, user) == []


async def test_cvat_delete_shared_function_only_releases_claim(factory) -> None:
    fn = _model_name()
    user, _project, _mv_id = await _seed(factory)
    other, _p2, _mv2 = await _seed(factory)
    await _own(factory, user, fn)
    await _own(factory, other, fn)
    with respx.mock:  # no routes: the Nuclio function must stay
        async with _client(factory, user) as c:
            res = await c.delete(f"/cvat/models/{fn}")
    assert res.status_code == 200, res.text
    assert res.json() == {"deleted": fn}
    assert await _owned(factory, user) == []
    assert len(await _owned(factory, other)) == 1


async def test_cvat_delete_deployer_error_keeps_claim(factory) -> None:
    fn = _model_name()
    user, _project, _mv_id = await _seed(factory)
    await _own(factory, user, fn)
    with respx.mock as mock:
        mock.delete(f"{cvat.DEPLOYER_URL}/models/{fn}").mock(
            return_value=httpx.Response(500, text="nope")
        )
        async with _client(factory, user) as c:
            res = await c.delete(f"/cvat/models/{fn}")
    assert res.status_code == 502, res.text
    assert len(await _owned(factory, user)) == 1
