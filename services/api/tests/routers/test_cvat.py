"""Router tests for the CVAT / model-deployer surface (``routers/cvat.py``).

Covers the CVAT endpoints, all behind ``get_current_user`` and scoped to the
caller's org (ownership of deployed functions lives in ``cvat_deployments``):

- ``POST /models/{id}/cvat-deploy`` — fetch presigned weights, then POST to the
  deployer ``/deploy``. 502 on weights-fetch failure or deployer error, 404 on
  unknown model.
- ``GET /cvat/models`` — proxy the deployer ``/models``; 502 on non-200.
- ``POST /projects/{id}/cvat-annotate`` — multipart upload proxied to the
  deployer ``/annotate``; 502 on non-200.

Ownership is exclusive (one live row per function name): deploys reserve the
name before calling the deployer, a racing or later org gets 409, and a failed
deploy releases its reservation.

The deployer HTTP calls (and the presigned weights GET) are mocked with respx.
``get_storage`` is faked to return a stable presigned URL that respx also mocks.
The cvat router reads ``DEPLOYER_URL`` from settings at import time, so tests
reference ``cvat.DEPLOYER_URL`` rather than re-reading settings.

ModelVersion has three NOT NULL foreign keys (blob_hash, trained_on_commit_id,
training_container_id), so each row is seeded inline with a Blob, Dataset,
Commit, and TrainingContainer — mirroring test_models.py.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio
import respx
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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


async def _live(factory, function_name: str) -> list[CvatDeployment]:
    """Active ownership rows for ``function_name``, across every org."""
    async with factory() as s:
        r = await s.execute(
            select(CvatDeployment).where(
                CvatDeployment.function_name == function_name,
                CvatDeployment.deleted_at.is_(None),
            )
        )
        return list(r.scalars().all())


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
    assert await _owned(factory, user) == []


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
    # The name was reserved before the deployer ran; the failure releases it.
    assert await _owned(factory, user) == []


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
    yolo, sam = _model_name(), _model_name()
    await _own(factory, user, yolo)
    await _own(factory, other, sam)
    payload = [{"id": yolo, "name": "YOLOv8"}, {"id": sam, "name": "SAM"}]
    with respx.mock as mock:
        mock.get(f"{cvat.DEPLOYER_URL}/models").mock(return_value=httpx.Response(200, json=payload))
        async with _client(factory, user) as c:
            res = await c.get("/cvat/models")
    assert res.status_code == 200, res.text
    # Only the caller's org's function: the other org's "sam" is hidden.
    assert res.json() == [{"id": yolo, "name": "YOLOv8"}]


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
    fn = _model_name()
    await _own(factory, user, fn)
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
                data={"task_name": "my-task", "function_id": fn, "threshold": "0.3"},
                files={"files": ("a.jpg", b"\xff\xd8\xff fake", "image/jpeg")},
            )
    assert res.status_code == 200, res.text
    assert res.json() == {"task_id": 1, "job_id": 2, "cvat_url": "http://x"}


async def test_cvat_annotate_foreign_project_404(factory) -> None:
    user, _project, _mv_id = await _seed(factory)
    fn = _model_name()
    await _own(factory, user, fn)
    _other, other_project, _mv2 = await _seed(factory)
    with respx.mock:  # no routes: the deployer must not be reached
        async with _client(factory, user) as c:
            res = await c.post(
                f"/projects/{other_project.id}/cvat-annotate",
                data={"task_name": "t", "function_id": fn},
                files={"files": ("a.jpg", b"\xff\xd8\xff fake", "image/jpeg")},
            )
    assert res.status_code == 404, res.text


async def test_cvat_annotate_foreign_function_404(factory) -> None:
    user, project, _mv_id = await _seed(factory)
    other, _p2, _mv2 = await _seed(factory)
    fn = _model_name()
    await _own(factory, other, fn)
    with respx.mock:  # no routes: the deployer must not be reached
        async with _client(factory, user) as c:
            res = await c.post(
                f"/projects/{project.id}/cvat-annotate",
                data={"task_name": "t", "function_id": fn},
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


async def test_cvat_delete_deployer_error_keeps_claim(factory) -> None:
    """A failed delete must not release the claim, so the delete can be retried."""
    fn = _model_name()
    user, _project, _mv_id = await _seed(factory)
    await _own(factory, user, fn)
    with respx.mock(assert_all_called=True) as mock:
        # First call: the deployer fails. Second call (the retry): it succeeds.
        route = mock.delete(f"{cvat.DEPLOYER_URL}/models/{fn}").mock(
            side_effect=[
                httpx.Response(500, text="nope"),
                httpx.Response(200, json={"deleted": fn}),
            ]
        )
        async with _client(factory, user) as c:
            failed = await c.delete(f"/cvat/models/{fn}")
            owned_after_failure = await _owned(factory, user)
            retried = await c.delete(f"/cvat/models/{fn}")
    assert failed.status_code == 502, failed.text
    assert "nope" in failed.json()["detail"]
    # Claim survives the failure, so the org can still see and retry the delete...
    assert len(owned_after_failure) == 1
    # ...and the retry goes through and only then releases it.
    assert retried.status_code == 200, retried.text
    assert route.call_count == 2
    assert await _owned(factory, user) == []


# ---------------------------------------------------------------------------
# Exclusive ownership: one live owner per function, reserved before deploying
# ---------------------------------------------------------------------------


def _deploy_ok(name: str) -> httpx.Response:
    return httpx.Response(200, json={"function_name": f"pth-custom-{name}"})


async def test_cvat_deploy_concurrent_orgs_one_wins(factory) -> None:
    """Two orgs racing to deploy the same name: one deploys, the other gets 409."""
    name = _model_name()
    a, _pa, mv_a = await _seed(factory)
    b, _pb, mv_b = await _seed(factory)

    async def _slow_deploy(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.3)  # hold the deploy open while the other org arrives
        return _deploy_ok(name)

    async def _deploy(user: User, mv_id: uuid.UUID) -> httpx.Response:
        async with _client(factory, user) as c:
            return await c.post(f"/models/{mv_id}/cvat-deploy?model_name={name}")

    with respx.mock as mock:
        mock.get(WEIGHTS_URL).mock(return_value=httpx.Response(200, content=b"weights"))
        route = mock.post(f"{cvat.DEPLOYER_URL}/deploy").mock(side_effect=_slow_deploy)
        results = await asyncio.gather(_deploy(a, mv_a), _deploy(b, mv_b))

    assert sorted(r.status_code for r in results) == [200, 409], [r.text for r in results]
    assert route.call_count == 1
    live = await _live(factory, f"pth-custom-{name}")
    assert len(live) == 1
    winner = a if results[0].status_code == 200 else b
    assert live[0].org_id == winner.org_id


async def test_cvat_reserve_racing_insert_is_409(factory) -> None:
    """The unique index, not the existence check, decides a race both requests pass.

    Org A's insert is flushed but not committed, so org B's existence check sees
    nothing and B's insert blocks on the index; once A commits, B's insert fails
    and must surface as a 409, not a 500.
    """
    fn = f"pth-custom-{_model_name()}"
    a, _pa, _mva = await _seed(factory)
    b, _pb, _mvb = await _seed(factory)
    async with factory() as sa, factory() as sb:
        sa.add(CvatDeployment(org_id=a.org_id, function_name=fn, model_name=fn))
        await sa.flush()
        task = asyncio.create_task(cvat._reserve_function(fn, fn, b, sb))
        await asyncio.sleep(0.5)
        assert not task.done(), "B should be blocked on the unique index"
        await sa.commit()
        with pytest.raises(HTTPException) as exc:
            await task
    assert exc.value.status_code == 409
    assert [d.org_id for d in await _live(factory, fn)] == [a.org_id]


async def test_cvat_deployments_unique_live_function_name(factory) -> None:
    """The schema itself refuses a second live owner for a function."""
    fn = f"pth-custom-{_model_name()}"
    a, _pa, _mva = await _seed(factory)
    b, _pb, _mvb = await _seed(factory)
    await _own(factory, a, fn)
    with pytest.raises(IntegrityError):
        await _own(factory, b, fn)


async def test_cvat_deploy_failure_releases_name_for_other_org(factory) -> None:
    name = _model_name()
    a, _pa, mv_a = await _seed(factory)
    b, _pb, mv_b = await _seed(factory)
    with respx.mock as mock:
        mock.get(WEIGHTS_URL).mock(return_value=httpx.Response(200, content=b"weights"))
        mock.post(f"{cvat.DEPLOYER_URL}/deploy").mock(
            side_effect=[httpx.Response(500, text="boom"), _deploy_ok(name)]
        )
        async with _client(factory, a) as c:
            failed = await c.post(f"/models/{mv_a}/cvat-deploy?model_name={name}")
        async with _client(factory, b) as c:
            ok = await c.post(f"/models/{mv_b}/cvat-deploy?model_name={name}")
    assert failed.status_code == 502, failed.text
    assert ok.status_code == 200, ok.text
    assert [d.org_id for d in await _live(factory, f"pth-custom-{name}")] == [b.org_id]


async def test_cvat_deploy_connect_error_releases_name(factory) -> None:
    name = _model_name()
    user, _project, mv_id = await _seed(factory)
    with respx.mock as mock:
        mock.get(WEIGHTS_URL).mock(return_value=httpx.Response(200, content=b"weights"))
        mock.post(f"{cvat.DEPLOYER_URL}/deploy").mock(
            side_effect=[httpx.ConnectError("down"), _deploy_ok(name)]
        )
        async with _client(factory, user) as c:
            failed = await c.post(f"/models/{mv_id}/cvat-deploy?model_name={name}")
            owned_after_failure = await _owned(factory, user)
            retried = await c.post(f"/models/{mv_id}/cvat-deploy?model_name={name}")
    assert failed.status_code == 503, failed.text
    assert owned_after_failure == []
    assert retried.status_code == 200, retried.text
    assert len(await _owned(factory, user)) == 1


async def test_cvat_deploy_file_failure_releases_name(factory) -> None:
    name = _model_name()
    a, _pa, _mva = await _seed(factory)
    b, _pb, _mvb = await _seed(factory)
    upload = {"file": ("m.pt", b"weights", "application/octet-stream")}
    with respx.mock as mock:
        mock.post(f"{cvat.DEPLOYER_URL}/deploy").mock(
            side_effect=[httpx.Response(500, text="boom"), _deploy_ok(name)]
        )
        async with _client(factory, a) as c:
            failed = await c.post(f"/cvat/deploy?model_name={name}", files=upload)
        async with _client(factory, b) as c:
            ok = await c.post(f"/cvat/deploy?model_name={name}", files=upload)
    assert failed.status_code == 502, failed.text
    assert ok.status_code == 200, ok.text
    assert [d.org_id for d in await _live(factory, f"pth-custom-{name}")] == [b.org_id]


async def test_cvat_redeploy_failure_keeps_existing_ownership(factory) -> None:
    """A failed same-org update must not drop the org's claim on its live function."""
    name = _model_name()
    user, _project, mv_id = await _seed(factory)
    with respx.mock as mock:
        mock.get(WEIGHTS_URL).mock(return_value=httpx.Response(200, content=b"weights"))
        mock.post(f"{cvat.DEPLOYER_URL}/deploy").mock(
            side_effect=[_deploy_ok(name), httpx.Response(500, text="boom")]
        )
        async with _client(factory, user) as c:
            first = await c.post(f"/models/{mv_id}/cvat-deploy?model_name={name}")
            second = await c.post(f"/models/{mv_id}/cvat-deploy?model_name={name}")
    assert first.status_code == 200, first.text
    assert second.status_code == 502, second.text
    assert len(await _owned(factory, user)) == 1


async def test_cvat_delete_frees_name_for_other_org(factory) -> None:
    name = _model_name()
    fn = f"pth-custom-{name}"
    a, _pa, _mva = await _seed(factory)
    b, _pb, mv_b = await _seed(factory)
    await _own(factory, a, fn)
    with respx.mock as mock:
        mock.delete(f"{cvat.DEPLOYER_URL}/models/{fn}").mock(
            return_value=httpx.Response(200, json={"deleted": fn})
        )
        mock.get(WEIGHTS_URL).mock(return_value=httpx.Response(200, content=b"weights"))
        mock.post(f"{cvat.DEPLOYER_URL}/deploy").mock(return_value=_deploy_ok(name))
        async with _client(factory, a) as c:
            deleted = await c.delete(f"/cvat/models/{fn}")
        async with _client(factory, b) as c:
            deployed = await c.post(f"/models/{mv_b}/cvat-deploy?model_name={name}")
    assert deleted.status_code == 200, deleted.text
    assert deployed.status_code == 200, deployed.text
    assert [d.org_id for d in await _live(factory, fn)] == [b.org_id]
