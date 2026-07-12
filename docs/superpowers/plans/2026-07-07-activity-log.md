# Activity Log (FEAT-4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface the existing `events` table as a read-only, org-scoped paginated activity feed at `GET /api/v1/events` (backend) and `/activity` (frontend page).

**Architecture:** Add `org_id` to `events` via an Alembic migration, thread it through all `emit_event` callers, add a new `GET /api/v1/events` endpoint with composite-keyset cursor pagination and optional filters, then build a React infinite-scroll Activity page and wire it into the sidebar.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.0 async / Alembic (backend); TypeScript / React 18 / TanStack Query `useInfiniteQuery` / Tailwind CSS (frontend)

## Global Constraints

- `org_id` column is added nullable — no single-step NOT NULL on existing tables (see migration `0002_ontologies_org_scoped.py` for the pattern).
- Cursor on this endpoint is composite: base64-encoded `"{created_at_iso}|{id}"`, ordered `DESC` on `(created_at, id)` — same pattern as `GET /projects/{id}/runs`.
- `Event` is append-only — no `deleted_at`; listing endpoint does not filter on it.
- `Event.actor_id` has no FK — the `actor_email` join is an explicit `outerjoin` on `users.id`.
- All frontend colors use semantic Tailwind tokens only (`text-iris-400`, `text-text-muted`, `bg-surface-2`, etc.) — no raw hex, rgb, or named CSS colors.
- Test commands run from `services/api/` (package installed editable via `pip install -e ".[dev]"`); no PYTHONPATH override needed.

---

### Task 1: DB migration + ORM model

**Files:**
- Create: `services/api/alembic/versions/0003_events_org_id.py`
- Modify: `services/api/src/cvops_api/db/models/runs.py` — `Event` class (lines 56–76)
- Modify: `services/api/tests/db/test_runs.py` — add one test

**Interfaces:**
- Produces: `Event.org_id: Mapped[Optional[uuid.UUID]]` — column exists and is writable

---

- [ ] **Step 1: Write the failing test**

Add to `services/api/tests/db/test_runs.py`:

```python
async def test_event_has_org_id(session: AsyncSession) -> None:
    """org_id column exists, is writable, and survives a flush/refresh."""
    import uuid
    from cvops_api.db.models.runs import Event

    org_id = uuid.uuid4()
    ev = Event(
        entity_type="run",
        entity_id=uuid.uuid4(),
        action="run.started",
        org_id=org_id,
    )
    session.add(ev)
    await session.flush()
    await session.refresh(ev)
    assert ev.org_id == org_id
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd services/api
pytest tests/db/test_runs.py::test_event_has_org_id -v
```

Expected: `FAIL` — `Event` has no `org_id` attribute yet.

- [ ] **Step 3: Add `org_id` to the `Event` ORM model**

In `services/api/src/cvops_api/db/models/runs.py`, replace the `Event` class with:

```python
class Event(Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    actor_id: Mapped[Optional[uuid.UUID]] = mapped_column(nullable=True)
    actor_type: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(nullable=True, index=False)

    __table_args__ = (
        Index("ix_events_entity", "entity_type", "entity_id", "created_at"),
        Index("ix_events_org_id_created_at", "org_id", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<Event id={self.id!r} action={self.action!r}"
            f" entity_type={self.entity_type!r} entity_id={self.entity_id!r}>"
        )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd services/api
pytest tests/db/test_runs.py::test_event_has_org_id -v
```

Expected: `PASS`

- [ ] **Step 5: Create the Alembic migration**

Create `services/api/alembic/versions/0003_events_org_id.py`:

```python
"""events: add org_id for multi-tenant activity feed

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-07 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0003'
down_revision: Union[str, None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('events', sa.Column('org_id', sa.Uuid(), nullable=True))
    op.create_index('ix_events_org_id_created_at', 'events', ['org_id', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_events_org_id_created_at', table_name='events')
    op.drop_column('events', 'org_id')
```

- [ ] **Step 6: Commit**

```bash
git add services/api/alembic/versions/0003_events_org_id.py \
        services/api/src/cvops_api/db/models/runs.py \
        services/api/tests/db/test_runs.py
git commit -m "feat: add org_id to events table for multi-tenant activity feed"
```

---

### Task 2: Thread org_id through emit_event and all callers

**Files:**
- Modify: `services/api/src/cvops_api/core/audit.py`
- Modify: `services/api/src/cvops_api/engine/coordinator.py`
- Modify: `services/api/src/cvops_api/routers/collections.py` (5 calls: lines 105, 149, 171, 213, 246)
- Modify: `services/api/src/cvops_api/routers/samples.py` (3 calls: lines 204, 226, 318)
- Modify: `services/api/src/cvops_api/routers/data_sources.py` (1 call: line 414)
- Modify: `services/api/src/cvops_api/routers/tags.py` (5 calls: lines 87, 113, 136, 175, 203)
- Modify: `services/api/src/cvops_api/routers/datasets.py` (1 call: line 643)
- Modify: `services/api/src/cvops_api/core/annotation_import.py` (1 call: line 132)
- Modify: `services/api/tests/core/test_audit.py` (add one test)

**Interfaces:**
- Consumes: `Event.org_id` (Task 1)
- Produces: `emit_event(..., org_id=uuid)` — every caller now sets this; events emitted without `org_id` keep `NULL` and are filtered out of the activity feed (correct — these are legacy or system rows with no user context)

---

- [ ] **Step 1: Write the failing test**

Add to `services/api/tests/core/test_audit.py`:

```python
async def test_emit_event_writes_org_id(session: AsyncSession) -> None:
    """org_id kwarg is stored in the events row."""
    import uuid
    from sqlalchemy import select
    from cvops_api.db.models.runs import Event

    org_id = uuid.uuid4()
    entity_id = uuid.uuid4()
    await emit_event(
        session,
        actor_id=None,
        actor_type="system",
        entity_type="run",
        entity_id=entity_id,
        action="run.started",
        org_id=org_id,
    )
    await session.flush()
    row = (await session.execute(
        select(Event).where(Event.entity_id == entity_id)
    )).scalar_one()
    assert row.org_id == org_id
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd services/api
pytest tests/core/test_audit.py::test_emit_event_writes_org_id -v
```

Expected: `FAIL` — `emit_event` does not accept `org_id` yet.

- [ ] **Step 3: Update `emit_event` in `core/audit.py`**

Replace the full function in `services/api/src/cvops_api/core/audit.py`:

```python
async def emit_event(
    session: AsyncSession,
    *,
    actor_id: str | uuid.UUID | None,
    actor_type: str,
    entity_type: str,
    entity_id: str | uuid.UUID,
    action: str,
    payload: dict[str, Any] | None = None,
    org_id: uuid.UUID | None = None,
) -> None:
    """
    Insert one row into the events table within the current transaction.
    Does not commit — the caller owns the transaction boundary.
    """
    await session.execute(
        text(
            """
            INSERT INTO events
                (id, actor_id, actor_type, entity_type, entity_id, action, payload, org_id, created_at)
            VALUES
                (:id, :actor_id, :actor_type, :entity_type, :entity_id, :action,
                 cast(:payload as jsonb), :org_id, now())
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "actor_id": str(actor_id) if actor_id else None,
            "actor_type": actor_type,
            "entity_type": entity_type,
            "entity_id": str(entity_id),
            "action": action,
            "payload": __import__("json").dumps(payload) if payload else "{}",
            "org_id": str(org_id) if org_id else None,
        },
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd services/api
pytest tests/core/test_audit.py -v
```

Expected: All 6 tests pass (new test passes, existing tests unaffected — `org_id` defaults to `None`).

- [ ] **Step 5: Update coordinator — add `Project` import and `org_id` to `_fail` / `_fail_child`**

In `services/api/src/cvops_api/engine/coordinator.py`:

**5a.** Add `Project` to imports (after `from cvops_api.db.models.runs import Run`):

```python
from cvops_api.db.models.projects import Project
from cvops_api.db.models.runs import Run
```

**5b.** Replace `_fail` with:

```python
async def _fail(
    session: AsyncSession,
    run: Run,
    actor_id: uuid.UUID,
    error: str,
    org_id: uuid.UUID | None = None,
) -> None:
    run.status = "failed"
    run.error = error
    run.finished_at = datetime.now(UTC)
    await session.flush()
    await emit_event(
        session,
        actor_id=actor_id,
        actor_type="system",
        entity_type="run",
        entity_id=run.id,
        action="run.failed",
        payload={"error": error},
        org_id=org_id,
    )
```

**5c.** Replace `_fail_child` with:

```python
async def _fail_child(
    session: AsyncSession,
    child: Run,
    parent: Run,
    actor_id: uuid.UUID,
    error: str,
    org_id: uuid.UUID | None = None,
) -> None:
    child.status = "failed"
    child.error = error
    child.finished_at = datetime.now(UTC)
    await _fail(session, parent, actor_id, f"Step '{child.step_id}' failed: {error}", org_id=org_id)
```

- [ ] **Step 6: Update `advance_workflow` — derive org_id and pass to all emit_event / _fail calls**

In `advance_workflow`, add this block immediately after the terminal-status early-return check (after `if parent.status in {"succeeded", "failed", "cancelled"}:` block):

```python
    proj = await session.get(Project, parent.project_id)
    org_id = proj.org_id if proj else None
```

Then add `org_id=org_id` to every `emit_event` and `_fail` call within `advance_workflow`:

- The idempotency-reuse `emit_event` (action `"run.succeeded"` on the child):
  ```python
  await emit_event(
      session,
      actor_id=actor_id,
      actor_type="system",
      entity_type="run",
      entity_id=child.id,
      action="run.succeeded",
      org_id=org_id,
  )
  ```

- The parent-finalize `emit_event` (action `"run.succeeded"` on the parent):
  ```python
  await emit_event(
      session,
      actor_id=actor_id,
      actor_type="system",
      entity_type="run",
      entity_id=parent_run_id,
      action="run.succeeded",
      org_id=org_id,
  )
  ```

- All `_fail(session, parent, actor_id, "...", org_id=org_id)` calls (there are 4 — config invalid, unknown step, input resolution, cycle, no-definition).

- [ ] **Step 7: Update `process_step` — derive org_id and pass to all emit_event / _fail_child calls**

In `process_step`, add org_id derivation after the `child.status != "pending"` guard (after the guard that returns early if child is already claimed):

```python
    proj = await session.get(Project, child.project_id)
    org_id = proj.org_id if proj else None
```

Then add `org_id=org_id` to all emit_event calls in `process_step`:

- `run.started` emit:
  ```python
  await emit_event(
      session,
      actor_id=actor_id,
      actor_type="system",
      entity_type="run",
      entity_id=child.id,
      action="run.started",
      org_id=org_id,
  )
  ```

- `run.waiting` emit (in the `GateException` handler):
  ```python
  await emit_event(
      session,
      actor_id=actor_id,
      actor_type="system",
      entity_type="run",
      entity_id=child.id,
      action="run.waiting",
      org_id=org_id,
  )
  ```

- `run.succeeded` emit:
  ```python
  await emit_event(
      session,
      actor_id=actor_id,
      actor_type="system",
      entity_type="run",
      entity_id=child.id,
      action="run.succeeded",
      org_id=org_id,
  )
  ```

- `_fail_child` calls (both the rollback-path and the normal-path):
  ```python
  await _fail_child(session, c2, p2, actor_id, str(exc), org_id=org_id)
  ```

- [ ] **Step 8: Update all router callers — add `org_id=current_user.org_id`**

For every `emit_event(...)` call in these files, add `org_id=current_user.org_id` as a keyword argument. Each function already has `current_user: User = Depends(get_current_user)` in its signature.

**`routers/collections.py`** — 5 calls:
- `create_collection` (line ~105)
- `update_collection` (line ~149)
- `delete_collection` (line ~171)
- `add_collection_samples` (line ~213)
- `remove_collection_samples` (line ~246)

**`routers/samples.py`** — 3 calls:
- `update_sample` (line ~204)
- `delete_sample` (line ~226)
- `bulk_sample_action` (line ~318)

**`routers/data_sources.py`** — 1 call:
- `confirm_images` (line ~414)

**`routers/tags.py`** — 5 calls:
- `create_tag` (line ~87)
- `update_tag` (line ~113)
- `delete_tag` (line ~136)
- `add_sample_tags` (line ~175)
- `remove_sample_tag` (line ~203)

**`routers/datasets.py`** — 1 call:
- `from_samples` commit endpoint (line ~643)

**`core/annotation_import.py`** — 1 call:
- `import_annotated_images` (line ~132) — uses `project.org_id` (the function receives `project: Project` as a parameter)
  ```python
  await emit_event(
      session,
      actor_id=str(actor_id),
      actor_type="user",
      entity_type="data_source",
      entity_id=source.id,
      action="images.uploaded_annotated",
      payload={"count": len(sample_ids), "annotated": annotated, "group": group},
      org_id=project.org_id,
  )
  ```

- [ ] **Step 9: Run the full test suite**

```bash
cd services/api
pytest tests/ -q
```

Expected: All tests pass. The new `org_id` param defaults to `None`, so existing tests that don't pass it still pass.

- [ ] **Step 10: Commit**

```bash
git add services/api/src/cvops_api/core/audit.py \
        services/api/src/cvops_api/engine/coordinator.py \
        services/api/src/cvops_api/routers/collections.py \
        services/api/src/cvops_api/routers/samples.py \
        services/api/src/cvops_api/routers/data_sources.py \
        services/api/src/cvops_api/routers/tags.py \
        services/api/src/cvops_api/routers/datasets.py \
        services/api/src/cvops_api/core/annotation_import.py \
        services/api/tests/core/test_audit.py
git commit -m "feat: thread org_id through emit_event and all callers"
```

---

### Task 3: GET /api/v1/events endpoint

**Files:**
- Modify: `services/api/src/cvops_api/schemas/runs.py` — add `ActivityEventOut`
- Create: `services/api/src/cvops_api/routers/events.py`
- Modify: `services/api/src/cvops_api/main.py` — import + mount
- Create: `services/api/tests/routers/test_events.py`

**Interfaces:**
- Consumes: `Event.org_id` (Task 1), `emit_event` with org_id (Task 2)
- Produces: `GET /api/v1/events` → `{"items": [...ActivityEventOut], "next_cursor": str | null}`

---

- [ ] **Step 1: Write the failing tests**

Create `services/api/tests/routers/test_events.py`:

```python
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
    """Two orgs with one user each. `n` events for org1 with distinct timestamps.
    Returns (user1, user2_different_org, event_ids newest-first).
    """
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd services/api
pytest tests/routers/test_events.py -v
```

Expected: `ImportError` — `routers/events.py` doesn't exist yet.

- [ ] **Step 3: Add `ActivityEventOut` to `schemas/runs.py`**

In `services/api/src/cvops_api/schemas/runs.py`, add after the existing `EventOut` class:

```python
class ActivityEventOut(BaseModel):
    """EventOut extended with actor_email for the org-wide activity feed."""
    id: uuid.UUID
    created_at: datetime
    actor_id: uuid.UUID | None = None
    actor_type: str | None = None
    actor_email: str | None = None
    entity_type: str
    entity_id: uuid.UUID
    action: str
    payload: dict[str, Any] | None = None
```

- [ ] **Step 4: Create `routers/events.py`**

Create `services/api/src/cvops_api/routers/events.py`:

```python
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from cvops_api.core.auth import get_current_user
from cvops_api.core.pagination import encode_cursor, decode_cursor_parts
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import User
from cvops_api.db.models.runs import Event
from cvops_api.schemas.runs import ActivityEventOut
from cvops_api.schemas.samples import CursorPage

router = APIRouter()


@router.get("/events", response_model=CursorPage[ActivityEventOut])
async def list_events(
    entity_type: str | None = Query(None),
    action: str | None = Query(None),
    cursor: str | None = Query(None),
    limit: int = Query(50, le=200),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> CursorPage[ActivityEventOut]:
    """Org-scoped activity feed, newest-first, composite-keyset cursor paginated."""
    # Actor email is not a FK — explicit outerjoin.
    q = (
        select(Event, User.email.label("actor_email"))
        .outerjoin(User, Event.actor_id == User.id)
        .where(Event.org_id == current_user.org_id)
    )
    if entity_type is not None:
        q = q.where(Event.entity_type == entity_type)
    if action is not None:
        q = q.where(Event.action == action)
    if cursor is not None:
        ts_str, id_str = decode_cursor_parts(cursor)
        try:
            cursor_ts = datetime.fromisoformat(ts_str)
            cursor_id = uuid.UUID(id_str)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid pagination cursor") from exc
        q = q.where(tuple_(Event.created_at, Event.id) < (cursor_ts, cursor_id))

    q = q.order_by(Event.created_at.desc(), Event.id.desc()).limit(limit + 1)

    rows = (await session.execute(q)).all()

    next_cursor: str | None = None
    if len(rows) == limit + 1:
        last_ev = rows[limit - 1][0]
        next_cursor = encode_cursor(f"{last_ev.created_at.isoformat()}|{last_ev.id}")
        rows = rows[:limit]

    items = [
        ActivityEventOut(
            id=ev.id,
            created_at=ev.created_at,
            actor_id=ev.actor_id,
            actor_type=ev.actor_type,
            actor_email=email,
            entity_type=ev.entity_type,
            entity_id=ev.entity_id,
            action=ev.action,
            payload=ev.payload,
        )
        for ev, email in rows
    ]
    return CursorPage(items=items, next_cursor=next_cursor)
```

- [ ] **Step 5: Mount the router in `main.py`**

In `services/api/src/cvops_api/main.py`, add `events` to the router import block:

```python
from cvops_api.routers import (
    auth,
    orgs,
    projects,
    data_sources,
    samples,
    collections,
    tags,
    ontologies,
    datasets,
    workflows,
    runs,
    models,
    training_containers,
    registry as registry_router,
    internal,
    cvat,
    viewer,
    events,
)
```

Add the mount line after the `runs` router line:

```python
app.include_router(events.router, prefix=API_V1, tags=["events"])
```

- [ ] **Step 6: Run the tests**

```bash
cd services/api
pytest tests/routers/test_events.py -v
```

Expected: All 6 tests pass.

- [ ] **Step 7: Run full suite to check for regressions**

```bash
cd services/api
pytest tests/ -q
```

Expected: All tests pass.

- [ ] **Step 8: Commit**

```bash
git add services/api/src/cvops_api/schemas/runs.py \
        services/api/src/cvops_api/routers/events.py \
        services/api/src/cvops_api/main.py \
        services/api/tests/routers/test_events.py
git commit -m "feat: add GET /api/v1/events activity feed endpoint"
```

---

### Task 4: Frontend API hook

**Files:**
- Create: `services/frontend/src/api/events.ts`

**Interfaces:**
- Consumes: `GET /api/v1/events` (Task 3)
- Produces: `useEvents(filters: EventFilters)` → TanStack `InfiniteData<CursorPage<ActivityEventOut>>`, `ActivityEventOut` TypeScript type

---

- [ ] **Step 1: Create `src/api/events.ts`**

```typescript
import { useInfiniteQuery } from '@tanstack/react-query'
import { client } from '../lib/client'
import type { CursorPage } from './samples'

export interface ActivityEventOut {
  id: string
  created_at: string
  actor_id: string | null
  actor_type: string | null
  actor_email: string | null
  entity_type: string
  entity_id: string
  action: string
  payload: Record<string, unknown> | null
}

export interface EventFilters {
  entity_type?: string
  action?: string
}

export function useEvents(filters: EventFilters = {}) {
  return useInfiniteQuery<CursorPage<ActivityEventOut>>({
    queryKey: ['events', filters],
    queryFn: async ({ pageParam }) => {
      const params = new URLSearchParams({ limit: '50' })
      if (pageParam) params.set('cursor', pageParam as string)
      if (filters.entity_type) params.set('entity_type', filters.entity_type)
      if (filters.action) params.set('action', filters.action)
      const { data } = await client.get<CursorPage<ActivityEventOut>>(`/events?${params}`)
      return data
    },
    initialPageParam: null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  })
}
```

- [ ] **Step 2: Verify TypeScript compiles**

```bash
cd services/frontend
npm run typecheck
```

Expected: No errors.

- [ ] **Step 3: Commit**

```bash
git add services/frontend/src/api/events.ts
git commit -m "feat: add useEvents infinite-query hook"
```

---

### Task 5: Activity page, route, and sidebar nav

**Files:**
- Create: `services/frontend/src/pages/Activity.tsx`
- Modify: `services/frontend/src/App.tsx` — add `/activity` route
- Modify: `services/frontend/src/components/layout/Sidebar.tsx` — add nav link

**Interfaces:**
- Consumes: `useEvents`, `ActivityEventOut` (Task 4)
- Produces: `/activity` page in the app, visible in sidebar after Label Sets

---

- [ ] **Step 1: Create `src/pages/Activity.tsx`**

```tsx
import { useRef, useEffect, useState } from 'react'
import { useEvents } from '../api/events'
import type { ActivityEventOut } from '../api/events'
import { EmptyState, Button } from '../components/ui'

const ENTITY_FILTERS: { label: string; value?: string }[] = [
  { label: 'All', value: undefined },
  { label: 'Run', value: 'run' },
  { label: 'Commit', value: 'commit' },
  { label: 'Data Source', value: 'data_source' },
  { label: 'Sample', value: 'sample' },
  { label: 'Annotation', value: 'annotation_revision' },
]

const ENTITY_COLOR: Record<string, string> = {
  run: 'text-iris-400',
  commit: 'text-lime-400',
  data_source: 'text-amber-400',
  sample: 'text-sky-400',
  annotation_revision: 'text-pink-400',
}

const ENTITY_ICON: Record<string, string> = {
  run: '▶',
  commit: '◈',
  data_source: '⬆',
  sample: '⬡',
  annotation_revision: '✎',
}

const EVENT_LABELS: Record<string, string> = {
  'run/run.started': 'Run started',
  'run/run.succeeded': 'Run succeeded',
  'run/run.failed': 'Run failed',
  'run/run.waiting': 'Run waiting at gate',
  'commit/created': 'Dataset committed',
  'commit/branch.advanced': 'Branch advanced',
  'data_source/created': 'Data source uploaded',
  'data_source/images.uploaded': 'Images uploaded',
  'data_source/images.uploaded_annotated': 'Annotated images imported',
  'annotation_revision/created': 'Annotations saved',
  'sample/sample.updated': 'Sample updated',
  'sample/sample.deleted': 'Sample deleted',
}

function describeEvent(ev: ActivityEventOut): string {
  return EVENT_LABELS[`${ev.entity_type}/${ev.action}`] ?? `${ev.entity_type} ${ev.action}`
}

function actorLabel(ev: ActivityEventOut): string {
  if (ev.actor_email) return ev.actor_email.split('@')[0]
  return ev.actor_type ?? 'system'
}

function relativeTime(isoString: string): string {
  const s = Math.floor((Date.now() - new Date(isoString).getTime()) / 1000)
  if (s < 60) return `${s}s ago`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

function EventRow({ ev }: { ev: ActivityEventOut }) {
  const color = ENTITY_COLOR[ev.entity_type] ?? 'text-text-muted'
  const icon = ENTITY_ICON[ev.entity_type] ?? '·'
  return (
    <div className="flex items-start gap-3 px-4 py-3 border-b border-border last:border-0">
      <span className={`mt-0.5 text-base w-5 flex-shrink-0 ${color}`}>{icon}</span>
      <div className="min-w-0 flex-1">
        <p className="text-sm text-text-primary">{describeEvent(ev)}</p>
        <p className="text-xs text-text-muted mt-0.5">{actorLabel(ev)}</p>
      </div>
      <span className="text-xs text-text-muted flex-shrink-0 mt-0.5" title={ev.created_at}>
        {relativeTime(ev.created_at)}
      </span>
    </div>
  )
}

function Skeleton() {
  return (
    <div>
      {Array.from({ length: 5 }).map((_, i) => (
        <div key={i} className="flex gap-3 px-4 py-3 border-b border-border">
          <div className="w-5 h-4 bg-surface-3 rounded animate-pulse flex-shrink-0 mt-0.5" />
          <div className="flex-1 space-y-1.5">
            <div className="h-3 bg-surface-3 rounded w-2/3 animate-pulse" />
            <div className="h-2.5 bg-surface-3 rounded w-1/3 animate-pulse" />
          </div>
          <div className="w-10 h-3 bg-surface-3 rounded animate-pulse flex-shrink-0 mt-0.5" />
        </div>
      ))}
    </div>
  )
}

export default function Activity() {
  const [entityType, setEntityType] = useState<string | undefined>(undefined)
  const [action, setAction] = useState('')
  const sentinelRef = useRef<HTMLDivElement>(null)

  const { data, isLoading, hasNextPage, isFetchingNextPage, fetchNextPage } = useEvents({
    entity_type: entityType,
    action: action.trim() || undefined,
  })

  const events = data?.pages.flatMap(p => p.items) ?? []

  useEffect(() => {
    if (!sentinelRef.current) return
    const obs = new IntersectionObserver(
      ([entry]) => { if (entry.isIntersecting && hasNextPage) fetchNextPage() },
      { threshold: 0.1 },
    )
    obs.observe(sentinelRef.current)
    return () => obs.disconnect()
  }, [hasNextPage, fetchNextPage])

  return (
    <div className="p-6 max-w-3xl mx-auto">
      <h2 className="text-xl font-bold text-text-primary mb-4">Activity</h2>

      <div className="flex flex-wrap items-center gap-1.5 mb-4">
        {ENTITY_FILTERS.map(f => (
          <Button
            key={f.label}
            size="sm"
            variant={entityType === f.value ? 'primary' : 'secondary'}
            onClick={() => setEntityType(f.value)}
          >
            {f.label}
          </Button>
        ))}
        <input
          type="text"
          placeholder="Filter by action…"
          value={action}
          onChange={e => setAction(e.target.value)}
          className="ml-2 px-3 py-1 text-sm rounded-lg border border-border bg-surface-2 text-text-primary placeholder:text-text-muted focus:outline-none focus:ring-2 focus:ring-focus"
        />
      </div>

      <div className="bg-surface-2 rounded-xl border border-border overflow-hidden">
        {isLoading && <Skeleton />}

        {!isLoading && events.length === 0 && (
          <div className="p-8">
            <EmptyState
              title="No activity yet"
              description="Events will appear here as the system runs"
            />
          </div>
        )}

        {events.map(ev => <EventRow key={ev.id} ev={ev} />)}

        {isFetchingNextPage && <Skeleton />}

        <div ref={sentinelRef} className="h-1" />
      </div>
    </div>
  )
}
```

- [ ] **Step 2: Add the `/activity` route to `App.tsx`**

Add the import after the existing page imports in `services/frontend/src/App.tsx`:

```typescript
import Activity from './pages/Activity'
```

Inside the `<Route element={<RequireAuth><Layout /></RequireAuth>}>` block, add after the `/ontologies` route line:

```tsx
<Route path="/activity"                              element={<Activity />} />
```

- [ ] **Step 3: Add Activity nav link to `Sidebar.tsx`**

In `services/frontend/src/components/layout/Sidebar.tsx`, inside the global nav `<nav>` block (the one with All Projects / Deployed Models / Label Sets), add after the Label Sets `NavLink`:

```tsx
<NavLink to="/activity" className={navClass}>
  Activity
</NavLink>
```

- [ ] **Step 4: Verify TypeScript compiles with no errors**

```bash
cd services/frontend
npm run typecheck
```

Expected: No errors.

- [ ] **Step 5: Verify lint passes**

```bash
cd services/frontend
npm run lint
```

Expected: No errors (max-warnings 0).

- [ ] **Step 6: Commit**

```bash
git add services/frontend/src/pages/Activity.tsx \
        services/frontend/src/App.tsx \
        services/frontend/src/components/layout/Sidebar.tsx
git commit -m "feat: add Activity feed page at /activity"
```
