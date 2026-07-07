"""Tests for GET /api/v1/events — org-scoped activity feed."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, UTC

import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cvops_api.core.auth import get_current_user
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import Org, User
from cvops_api.db.models.runs import Event
from cvops_api.routers import events as events_router


@pytest_asyncio.fixture
async def factory(postgres_url: str):
    engine = create_async_engine(postgres_url, echo=False)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


def _client(factory, current_user: User) -> AsyncClient:
    app = FastAPI()
    app.include_router(events_router.router)

    async def _session_dep():
        async with factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _session_dep
    app.dependency_overrides[get_current_user] = lambda: current_user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _seed(factory, *, n: int = 3) -> tuple[User, User, list[uuid.UUID]]:
    suffix = uuid.uuid4().hex[:8]
    base = datetime(2026, 1, 1, tzinfo=UTC)
    event_ids: list[uuid.UUID] = []
    async with factory() as s:
        org1 = Org(name=f"org1-{suffix}")
        org2 = Org(name=f"org2-{suffix}")
        s.add_all([org1, org2])
        await s.flush()
        user1 = User(org_id=org1.id, email=f"u1-{suffix}@test.com")
        user2 = User(org_id=org2.id, email=f"u2-{suffix}@test.com")
        s.add_all([user1, user2])
        await s.flush()
        for i in range(n):
            ev = Event(
                entity_type="run",
                entity_id=uuid.uuid4(),
                action="run.started",
                actor_id=user1.id,
                actor_type="user",
                org_id=org1.id,
                created_at=base + timedelta(seconds=i),
            )
            s.add(ev)
            await s.flush()
            event_ids.append(ev.id)
        await s.commit()
    return user1, user2, list(reversed(event_ids))  # newest-first


async def test_list_returns_org_events_newest_first(factory) -> None:
    user1, _user2, event_ids = await _seed(factory, n=3)
    async with _client(factory, user1) as c:
        resp = await c.get("/events")
    assert resp.status_code == 200
    ids = [item["id"] for item in resp.json()["items"]]
    assert ids == [str(eid) for eid in event_ids]


async def test_cross_org_events_invisible(factory) -> None:
    _user1, user2, _ids = await _seed(factory, n=3)
    async with _client(factory, user2) as c:
        resp = await c.get("/events")
    assert resp.status_code == 200
    assert resp.json()["items"] == []


async def test_actor_email_joined(factory) -> None:
    user1, _user2, _ids = await _seed(factory, n=1)
    async with _client(factory, user1) as c:
        resp = await c.get("/events")
    item = resp.json()["items"][0]
    assert item["actor_email"] == user1.email


async def test_cursor_pagination(factory) -> None:
    user1, _user2, event_ids = await _seed(factory, n=5)
    async with _client(factory, user1) as c:
        page1_resp = await c.get("/events?limit=3")
    page1 = page1_resp.json()
    assert len(page1["items"]) == 3
    assert page1["next_cursor"] is not None
    assert [i["id"] for i in page1["items"]] == [str(e) for e in event_ids[:3]]

    async with _client(factory, user1) as c:
        page2_resp = await c.get(f"/events?limit=3&cursor={page1['next_cursor']}")
    page2 = page2_resp.json()
    assert len(page2["items"]) == 2
    assert page2["next_cursor"] is None
    assert [i["id"] for i in page2["items"]] == [str(e) for e in event_ids[3:]]


async def test_filter_entity_type(factory) -> None:
    suffix = uuid.uuid4().hex[:8]
    async with factory() as s:
        org = Org(name=f"org-{suffix}")
        s.add(org)
        await s.flush()
        user = User(org_id=org.id, email=f"u-{suffix}@test.com")
        s.add(user)
        await s.flush()
        for etype in ("run", "commit", "run"):
            s.add(Event(entity_type=etype, entity_id=uuid.uuid4(), action="created", org_id=org.id))
        await s.commit()
    async with _client(factory, user) as c:
        resp = await c.get("/events?entity_type=commit")
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["entity_type"] == "commit"


async def test_filter_action(factory) -> None:
    suffix = uuid.uuid4().hex[:8]
    async with factory() as s:
        org = Org(name=f"org-{suffix}")
        s.add(org)
        await s.flush()
        user = User(org_id=org.id, email=f"u-{suffix}@test.com")
        s.add(user)
        await s.flush()
        for action in ("run.started", "run.succeeded", "run.started"):
            s.add(Event(entity_type="run", entity_id=uuid.uuid4(), action=action, org_id=org.id))
        await s.commit()
    async with _client(factory, user) as c:
        resp = await c.get("/events?action=run.succeeded")
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["action"] == "run.succeeded"
