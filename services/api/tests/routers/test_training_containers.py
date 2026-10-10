"""Router tests for the training-containers surface.

Covers list, create, get (404 + cross-org 404), patch, delete (soft-delete),
and the POST .../validate endpoint, which validates a candidate icd_config
payload against the container's stored icd_config (used as a JSON Schema).

Pattern mirrors test_projects/test_data_sources: a minimal app mounting only
the training_containers router with get_session/get_current_user overridden
onto the testcontainers Postgres.
"""

from __future__ import annotations

import uuid

import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cvops_api.core.auth import get_current_user
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import Org, User
from cvops_api.db.models.projects import Project
from cvops_api.db.models.models import TrainingContainer
from cvops_api.routers import training_containers


# A real JSON Schema, stored as the container's icd_config; /validate checks a
# candidate payload against it.
_SCHEMA = {
    "type": "object",
    "properties": {"epochs": {"type": "integer"}},
    "required": ["epochs"],
}


@pytest_asyncio.fixture
async def factory(postgres_url: str):
    engine = create_async_engine(postgres_url, echo=False)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


def _client(factory, current_user: User) -> AsyncClient:
    app = FastAPI()
    app.include_router(training_containers.router)

    async def _get_session_dep():
        async with factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_session_dep
    app.dependency_overrides[get_current_user] = lambda: current_user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _seed(factory):
    """Create org/user/project. Returns (user, project)."""
    suffix = uuid.uuid4().hex[:8]
    async with factory() as s:
        org = Org(name=f"org-{suffix}")
        s.add(org)
        await s.flush()
        user = User(org_id=org.id, email=f"u-{suffix}@test.com")
        s.add(user)
        project = Project(org_id=org.id, name=f"proj-{suffix}")
        s.add(project)
        await s.commit()
        await s.refresh(user)
        await s.refresh(project)
        return user, project


def _create_body(name: str = "trainer") -> dict:
    return {
        "name": name,
        "description": "desc",
        "image": "registry/trainer:1",
        "icd_config": _SCHEMA,
        "icd_schema_version": "1.0",
    }


async def test_create_and_list(factory) -> None:
    user, project = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.post(f"/projects/{project.id}/training-containers", json=_create_body())
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["name"] == "trainer"
        assert body["image"] == "registry/trainer:1"
        assert body["icd_config"] == _SCHEMA

        listed = await c.get(f"/projects/{project.id}/training-containers")
        assert listed.status_code == 200, listed.text
        assert [tc["id"] for tc in listed.json()] == [body["id"]]


async def test_get_and_404(factory) -> None:
    user, project = await _seed(factory)
    async with _client(factory, user) as c:
        created = await c.post(f"/projects/{project.id}/training-containers", json=_create_body())
        tcid = created.json()["id"]

        got = await c.get(f"/training-containers/{tcid}")
        assert got.status_code == 200, got.text
        assert got.json()["id"] == tcid

        missing = await c.get(f"/training-containers/{uuid.uuid4()}")
        assert missing.status_code == 404, missing.text


async def test_get_cross_org_404(factory) -> None:
    owner, project = await _seed(factory)
    other, _p2 = await _seed(factory)
    async with _client(factory, owner) as c:
        created = await c.post(f"/projects/{project.id}/training-containers", json=_create_body())
        tcid = created.json()["id"]

    async with _client(factory, other) as c:
        res = await c.get(f"/training-containers/{tcid}")
    assert res.status_code == 404, res.text


async def test_patch(factory) -> None:
    user, project = await _seed(factory)
    async with _client(factory, user) as c:
        created = await c.post(f"/projects/{project.id}/training-containers", json=_create_body())
        tcid = created.json()["id"]

        res = await c.patch(
            f"/training-containers/{tcid}",
            json={"name": "renamed", "image": "registry/trainer:2"},
        )
        assert res.status_code == 200, res.text
        assert res.json()["name"] == "renamed"
        assert res.json()["image"] == "registry/trainer:2"

    async with factory() as s:
        refreshed = await s.get(TrainingContainer, uuid.UUID(tcid))
        assert refreshed.name == "renamed"
        assert refreshed.image == "registry/trainer:2"


async def test_delete_soft_deletes(factory) -> None:
    user, project = await _seed(factory)
    async with _client(factory, user) as c:
        created = await c.post(f"/projects/{project.id}/training-containers", json=_create_body())
        tcid = created.json()["id"]

        d = await c.delete(f"/training-containers/{tcid}")
        assert d.status_code == 204, d.text

        # Gone for reads.
        g = await c.get(f"/training-containers/{tcid}")
        assert g.status_code == 404, g.text

        # And not in the list.
        listed = await c.get(f"/projects/{project.id}/training-containers")
        assert [tc["id"] for tc in listed.json()] == []

    async with factory() as s:
        row = await s.get(TrainingContainer, uuid.UUID(tcid))
        assert row is not None
        assert row.deleted_at is not None


async def test_validate_valid_and_invalid(factory) -> None:
    user, project = await _seed(factory)
    async with _client(factory, user) as c:
        created = await c.post(f"/projects/{project.id}/training-containers", json=_create_body())
        tcid = created.json()["id"]

        # Valid candidate: satisfies the required "epochs" integer.
        ok = await c.post(
            f"/training-containers/{tcid}/validate",
            json={"icd_config": {"epochs": 10}},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["valid"] is True
        assert ok.json()["errors"] == []

        # Invalid candidate: wrong type for "epochs".
        bad = await c.post(
            f"/training-containers/{tcid}/validate",
            json={"icd_config": {"epochs": "lots"}},
        )
        assert bad.status_code == 200, bad.text
        assert bad.json()["valid"] is False
        assert len(bad.json()["errors"]) >= 1


async def test_create_under_foreign_project_404(factory) -> None:
    """Creating a container under another org's project is rejected (project
    scoping enforced on the create path, not just reads)."""
    _owner, project = await _seed(factory)
    other_user, _other_project = await _seed(factory)

    async with _client(factory, other_user) as c:
        res = await c.post(f"/projects/{project.id}/training-containers", json=_create_body())
        assert res.status_code == 404, res.text


async def test_list_cross_org_404(factory) -> None:
    """Listing under a foreign project 404s — the list endpoint scopes on the
    project's org, so a cross-org caller can't even enumerate."""
    owner, project = await _seed(factory)
    other_user, _other_project = await _seed(factory)
    async with _client(factory, owner) as c:
        await c.post(f"/projects/{project.id}/training-containers", json=_create_body())

    async with _client(factory, other_user) as c:
        res = await c.get(f"/projects/{project.id}/training-containers")
        assert res.status_code == 404, res.text
