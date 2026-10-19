"""Router tests for POST /projects/{id}/imports/upload-url and /imports.

Mounts only the imports router over testcontainers Postgres. The real
import_dataset + commit_dataset steps are registered so config validation
and queue routing run; their run() bodies are never invoked.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cvops_api.config import settings
from cvops_api.core.auth import get_current_user
from cvops_api.core.registry import registry
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import Org, User
from cvops_api.db.models.projects import Project
from cvops_api.db.models.ontologies import Ontology
from cvops_api.db.models.runs import Run
from cvops_api.routers import imports


@pytest_asyncio.fixture
async def factory(postgres_url: str):
    engine = create_async_engine(postgres_url, echo=False)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


@pytest.fixture
def import_steps():
    from cvops_steps.import_dataset import ImportDatasetStep
    from cvops_steps.commit_dataset import CommitDatasetStep

    steps = [ImportDatasetStep(), CommitDatasetStep()]
    for s in steps:
        registry.register(s)
    yield
    for s in steps:
        registry._store.pop(s.type_key, None)


def _client(factory, current_user: User) -> AsyncClient:
    app = FastAPI()
    app.include_router(imports.router)

    async def _session_dep():
        async with factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _session_dep
    app.dependency_overrides[get_current_user] = lambda: current_user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _seed(factory) -> tuple[User, Project, Ontology]:
    suffix = uuid.uuid4().hex[:8]
    async with factory() as s:
        org = Org(name=f"org-{suffix}")
        s.add(org)
        await s.flush()
        user = User(org_id=org.id, email=f"u-{suffix}@test.com")
        project = Project(org_id=org.id, name=f"proj-{suffix}")
        s.add(user)
        s.add(project)
        await s.flush()
        ontology = Ontology(org_id=org.id, name=f"ont-{suffix}", version=1)
        s.add(ontology)
        await s.commit()
        await s.refresh(user)
        await s.refresh(project)
        await s.refresh(ontology)
        return user, project, ontology


async def test_upload_url_returns_presigned_url(factory, import_steps) -> None:
    user, project, _ont = await _seed(factory)
    blob_hash = "sha256:" + "a" * 64

    with patch("cvops_api.routers.imports.get_storage") as mock_storage:
        mock_storage.return_value.get_presigned_put = AsyncMock(
            return_value="http://s3.example/presigned"
        )
        async with _client(factory, user) as c:
            res = await c.post(
                f"/projects/{project.id}/imports/upload-url",
                json={"blob_hash": blob_hash},
            )

    assert res.status_code == 200, res.text
    assert res.json()["upload_url"] == "http://s3.example/presigned"


async def test_upload_url_invalid_hash_422(factory, import_steps) -> None:
    user, project, _ont = await _seed(factory)

    async with _client(factory, user) as c:
        res = await c.post(
            f"/projects/{project.id}/imports/upload-url",
            json={"blob_hash": "not-a-hash"},
        )

    assert res.status_code == 422


async def test_import_creates_run_and_enqueues_step(factory, fake_redis, import_steps) -> None:
    user, project, ont = await _seed(factory)

    async with _client(factory, user) as c:
        res = await c.post(
            f"/projects/{project.id}/imports",
            json={
                "blob_hash": "sha256:" + "b" * 64,
                "format": "auto",
                "ontology_id": str(ont.id),
                "dataset_name": "My Import",
                "review": False,
            },
        )

    assert res.status_code == 201, res.text
    run_id = res.json()["id"]

    async with factory() as s:
        parent = await s.get(Run, uuid.UUID(run_id))
        assert parent is not None
        assert parent.kind == "workflow"
        assert parent.status == "pending"
        definition = parent.config["definition"]
        step_ids = {st["id"] for st in definition["steps"]}
        assert step_ids == {"import", "commit"}
        assert definition["edges"] == [{"from": "import", "to": "commit"}]

        children = (
            (await s.execute(select(Run).where(Run.parent_run_id == parent.id))).scalars().all()
        )
        assert len(children) == 1
        assert children[0].step_type == "step.import_dataset"
        assert children[0].status == "pending"

    assert await fake_redis.xlen("preprocessing") >= 1


async def test_import_missing_ontology_422(factory, import_steps) -> None:
    user, project, _ont = await _seed(factory)

    async with _client(factory, user) as c:
        res = await c.post(
            f"/projects/{project.id}/imports",
            json={"blob_hash": "sha256:" + "c" * 64},
        )

    assert res.status_code == 422


async def test_import_unknown_project_404(factory, import_steps) -> None:
    user, _project, ont = await _seed(factory)

    async with _client(factory, user) as c:
        res = await c.post(
            f"/projects/{uuid.uuid4()}/imports",
            json={
                "blob_hash": "sha256:" + "d" * 64,
                "ontology_id": str(ont.id),
            },
        )

    assert res.status_code == 404


# ── server-path import is allow-listed (#185) ──────────────────────────────


async def _runs_in(factory, project: Project) -> int:
    async with factory() as s:
        rows = (await s.execute(select(Run).where(Run.project_id == project.id))).scalars().all()
        return len(rows)


async def test_import_folder_path_disabled_by_default_422(factory, import_steps) -> None:
    user, project, ont = await _seed(factory)

    with patch.object(settings, "IMPORT_ROOTS", ""):
        async with _client(factory, user) as c:
            res = await c.post(
                f"/projects/{project.id}/imports",
                json={"folder_path": "/data/anything", "ontology_id": str(ont.id)},
            )

    assert res.status_code == 422, res.text
    assert "disabled" in res.json()["detail"]
    assert await _runs_in(factory, project) == 0


async def test_import_folder_path_outside_root_422(factory, import_steps, tmp_path) -> None:
    user, project, ont = await _seed(factory)
    allowed = tmp_path / "allowed"
    allowed.mkdir()

    with patch.object(settings, "IMPORT_ROOTS", str(allowed)):
        async with _client(factory, user) as c:
            for bad in ("/etc", str(tmp_path / "other"), str(allowed / ".." / "other")):
                res = await c.post(
                    f"/projects/{project.id}/imports",
                    json={"folder_path": bad, "ontology_id": str(ont.id)},
                )
                assert res.status_code == 422, (bad, res.text)
                assert "outside" in res.json()["detail"]

    assert await _runs_in(factory, project) == 0


async def test_import_folder_path_inside_root_creates_run(
    factory, fake_redis, import_steps, tmp_path
) -> None:
    user, project, ont = await _seed(factory)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    # The folder lives on the worker's disk, not the API's, so it need not exist here.
    folder = str(allowed / "cars")

    with patch.object(settings, "IMPORT_ROOTS", str(allowed)):
        async with _client(factory, user) as c:
            res = await c.post(
                f"/projects/{project.id}/imports",
                json={"folder_path": folder, "ontology_id": str(ont.id)},
            )

    assert res.status_code == 201, res.text
    async with factory() as s:
        child = (
            (await s.execute(select(Run).where(Run.step_type == "step.import_dataset")))
            .scalars()
            .all()
        )
        assert any(c.input_refs.get("folder_path") == folder for c in child)


@pytest.mark.parametrize(
    ("roots", "message"), [("relative/imports", "absolute"), ("/", "filesystem root")]
)
async def test_import_folder_path_misconfigured_roots_422(
    factory, import_steps, roots: str, message: str
) -> None:
    # A bad IMPORT_ROOTS fails closed: even "/etc" under a root of "/" is refused.
    user, project, ont = await _seed(factory)

    with patch.object(settings, "IMPORT_ROOTS", roots):
        async with _client(factory, user) as c:
            res = await c.post(
                f"/projects/{project.id}/imports",
                json={"folder_path": "/etc", "ontology_id": str(ont.id)},
            )

    assert res.status_code == 422, res.text
    assert message in res.json()["detail"]
    assert await _runs_in(factory, project) == 0
