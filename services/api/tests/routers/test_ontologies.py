"""Router tests for the ontologies router (org-scoped).

Endpoints tested: GET /ontologies, POST /ontologies, GET /ontologies/{id},
PATCH /ontologies/{id}, DELETE /ontologies/{id},
GET /ontologies/{id}/classes, POST /ontologies/{id}/classes,
PATCH /ontologies/{id}/classes/{class_id}, DELETE /ontologies/{id}/classes/{class_id}.
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
from cvops_api.db.models.ontologies import LabelClass, Ontology
from cvops_api.routers import ontologies


@pytest_asyncio.fixture
async def factory(postgres_url: str):
    engine = create_async_engine(postgres_url, echo=False)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


def _client(factory, current_user: User) -> AsyncClient:
    app = FastAPI()
    app.include_router(ontologies.router)

    async def _get_session_dep():
        async with factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_session_dep
    app.dependency_overrides[get_current_user] = lambda: current_user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _seed(factory) -> tuple[User, Org]:
    suffix = uuid.uuid4().hex[:8]
    async with factory() as s:
        org = Org(name=f"org-{suffix}")
        s.add(org)
        await s.flush()
        user = User(org_id=org.id, email=f"u-{suffix}@test.com")
        s.add(user)
        await s.commit()
        await s.refresh(user)
        await s.refresh(org)
        return user, org


# ── list ────────────────────────────────────────────────────────────────────


async def test_list_empty(factory) -> None:
    user, org = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.get("/ontologies")
    assert res.status_code == 200, res.text
    assert res.json() == []


async def test_list_excludes_soft_deleted(factory) -> None:
    user, org = await _seed(factory)
    async with factory() as s:
        ont = Ontology(org_id=org.id, name="live")
        dead = Ontology(org_id=org.id, name="gone")
        s.add_all([ont, dead])
        await s.flush()
        from datetime import UTC, datetime
        dead.deleted_at = datetime.now(UTC)
        await s.commit()

    async with _client(factory, user) as c:
        res = await c.get("/ontologies")
    assert res.status_code == 200
    names = [o["name"] for o in res.json()]
    assert "live" in names
    assert "gone" not in names


async def test_list_excludes_other_org(factory) -> None:
    user, org = await _seed(factory)
    other_user, other_org = await _seed(factory)
    async with factory() as s:
        s.add(Ontology(org_id=other_org.id, name="other-org-ont"))
        await s.commit()

    async with _client(factory, user) as c:
        res = await c.get("/ontologies")
    assert res.status_code == 200
    assert res.json() == []


# ── create ───────────────────────────────────────────────────────────────────


async def test_create_ontology(factory) -> None:
    user, org = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.post("/ontologies", json={"name": "detections"})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["name"] == "detections"
    assert body["org_id"] == str(org.id)
    assert body["version"] == 1


async def test_create_ontology_duplicate_name_409(factory) -> None:
    user, org = await _seed(factory)
    async with factory() as s:
        s.add(Ontology(org_id=org.id, name="dup"))
        await s.commit()
    async with _client(factory, user) as c:
        res = await c.post("/ontologies", json={"name": "dup"})
    assert res.status_code == 409


# ── get ──────────────────────────────────────────────────────────────────────


async def test_get_ontology(factory) -> None:
    user, org = await _seed(factory)
    async with factory() as s:
        ont = Ontology(org_id=org.id, name="get-test")
        s.add(ont)
        await s.commit()
        ont_id = ont.id

    async with _client(factory, user) as c:
        res = await c.get(f"/ontologies/{ont_id}")
    assert res.status_code == 200
    assert res.json()["name"] == "get-test"


async def test_get_ontology_404(factory) -> None:
    user, _ = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.get(f"/ontologies/{uuid.uuid4()}")
    assert res.status_code == 404


async def test_get_ontology_cross_org_404(factory) -> None:
    user, _ = await _seed(factory)
    other_user, other_org = await _seed(factory)
    async with factory() as s:
        ont = Ontology(org_id=other_org.id, name="private")
        s.add(ont)
        await s.commit()
        ont_id = ont.id

    async with _client(factory, user) as c:
        res = await c.get(f"/ontologies/{ont_id}")
    assert res.status_code == 404


# ── rename (PATCH) ───────────────────────────────────────────────────────────


async def test_rename_ontology(factory) -> None:
    user, org = await _seed(factory)
    async with factory() as s:
        ont = Ontology(org_id=org.id, name="old-name")
        s.add(ont)
        await s.commit()
        ont_id = ont.id

    async with _client(factory, user) as c:
        res = await c.patch(f"/ontologies/{ont_id}", json={"name": "new-name"})
    assert res.status_code == 200
    assert res.json()["name"] == "new-name"


async def test_rename_ontology_duplicate_409(factory) -> None:
    user, org = await _seed(factory)
    async with factory() as s:
        s.add(Ontology(org_id=org.id, name="taken"))
        ont = Ontology(org_id=org.id, name="mine")
        s.add(ont)
        await s.commit()
        ont_id = ont.id

    async with _client(factory, user) as c:
        res = await c.patch(f"/ontologies/{ont_id}", json={"name": "taken"})
    assert res.status_code == 409


# ── soft-delete ───────────────────────────────────────────────────────────────


async def test_delete_ontology(factory) -> None:
    user, org = await _seed(factory)
    async with factory() as s:
        ont = Ontology(org_id=org.id, name="to-delete")
        s.add(ont)
        await s.commit()
        ont_id = ont.id

    async with _client(factory, user) as c:
        res = await c.delete(f"/ontologies/{ont_id}")
    assert res.status_code == 204

    async with _client(factory, user) as c:
        res = await c.get(f"/ontologies/{ont_id}")
    assert res.status_code == 404


# ── label classes ─────────────────────────────────────────────────────────────


async def _make_ontology(factory, org_id) -> uuid.UUID:
    async with factory() as s:
        ont = Ontology(org_id=org_id, name=f"ont-{uuid.uuid4().hex[:6]}")
        s.add(ont)
        await s.commit()
        return ont.id


async def test_create_label_class(factory) -> None:
    user, org = await _seed(factory)
    ont_id = await _make_ontology(factory, org.id)
    async with _client(factory, user) as c:
        res = await c.post(
            f"/ontologies/{ont_id}/classes",
            json={"class_key": "car", "display_name": "Car", "color": "#FF0000", "sort_order": 0},
        )
    assert res.status_code == 201
    body = res.json()
    assert body["class_key"] == "car"
    assert body["ontology_id"] == str(ont_id)


async def test_create_label_class_duplicate_409(factory) -> None:
    user, org = await _seed(factory)
    ont_id = await _make_ontology(factory, org.id)
    async with factory() as s:
        s.add(LabelClass(ontology_id=ont_id, class_key="car", display_name="Car", sort_order=0))
        await s.commit()
    async with _client(factory, user) as c:
        res = await c.post(
            f"/ontologies/{ont_id}/classes",
            json={"class_key": "car", "display_name": "Car 2", "color": "#FF0000", "sort_order": 1},
        )
    assert res.status_code == 409


async def test_list_label_classes_ordered(factory) -> None:
    user, org = await _seed(factory)
    ont_id = await _make_ontology(factory, org.id)
    async with factory() as s:
        s.add(LabelClass(ontology_id=ont_id, class_key="b", display_name="B", sort_order=1))
        s.add(LabelClass(ontology_id=ont_id, class_key="a", display_name="A", sort_order=0))
        await s.commit()
    async with _client(factory, user) as c:
        res = await c.get(f"/ontologies/{ont_id}/classes")
    assert res.status_code == 200
    keys = [c["class_key"] for c in res.json()]
    assert keys == ["a", "b"]


async def test_update_label_class(factory) -> None:
    user, org = await _seed(factory)
    ont_id = await _make_ontology(factory, org.id)
    async with factory() as s:
        lc = LabelClass(ontology_id=ont_id, class_key="car", display_name="Car", sort_order=0)
        s.add(lc)
        await s.commit()
        lc_id = lc.id
    async with _client(factory, user) as c:
        res = await c.patch(
            f"/ontologies/{ont_id}/classes/{lc_id}",
            json={"display_name": "Automobile", "color": "#00FF00"},
        )
    assert res.status_code == 200
    body = res.json()
    assert body["display_name"] == "Automobile"
    assert body["color"] == "#00FF00"


async def test_delete_label_class(factory) -> None:
    user, org = await _seed(factory)
    ont_id = await _make_ontology(factory, org.id)
    async with factory() as s:
        lc = LabelClass(ontology_id=ont_id, class_key="car", display_name="Car", sort_order=0)
        s.add(lc)
        await s.commit()
        lc_id = lc.id
    async with _client(factory, user) as c:
        res = await c.delete(f"/ontologies/{ont_id}/classes/{lc_id}")
    assert res.status_code == 204
    async with _client(factory, user) as c:
        res = await c.get(f"/ontologies/{ont_id}/classes")
    assert res.json() == []


async def test_create_label_class_ontology_404(factory) -> None:
    user, _ = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.post(
            f"/ontologies/{uuid.uuid4()}/classes",
            json={"class_key": "car", "display_name": "Car", "color": "#FF0000", "sort_order": 0},
        )
    assert res.status_code == 404


async def test_list_classes_missing_ontology_404(factory) -> None:
    user, _ = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.get(f"/ontologies/{uuid.uuid4()}/classes")
    assert res.status_code == 404
