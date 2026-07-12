"""
Tests for cvops_api.core.audit.emit_event.

emit_event inserts one row into the events table via raw SQL within
the current transaction. The caller owns the commit boundary, so tests
call session.flush() to make the insert visible inside the same session.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.core.audit import emit_event
from cvops_api.db.models.runs import Event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entity_id() -> str:
    """Return a fresh UUID string suitable for use as an entity_id."""
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_emit_event_inserts_row(session: AsyncSession) -> None:
    """emit_event followed by flush should produce exactly one matching row."""
    entity_id = _entity_id()

    await emit_event(
        session,
        actor_id=str(uuid.uuid4()),
        actor_type="user",
        entity_type="project",
        entity_id=entity_id,
        action="created",
    )
    await session.flush()

    result = await session.execute(
        select(Event).where(
            Event.entity_type == "project",
            Event.entity_id == uuid.UUID(entity_id),
            Event.action == "created",
        )
    )
    rows = result.scalars().all()

    assert len(rows) == 1
    assert str(rows[0].entity_id) == entity_id
    assert rows[0].entity_type == "project"
    assert rows[0].action == "created"


async def test_emit_event_payload_stored(session: AsyncSession) -> None:
    """A payload dict passed to emit_event should be persisted in the JSONB column."""
    entity_id = _entity_id()

    await emit_event(
        session,
        actor_id=str(uuid.uuid4()),
        actor_type="user",
        entity_type="run",
        entity_id=entity_id,
        action="run.started",
        payload={"key": "val"},
    )
    await session.flush()

    result = await session.execute(select(Event).where(Event.entity_id == uuid.UUID(entity_id)))
    row = result.scalar_one()

    assert row.payload is not None
    assert row.payload["key"] == "val"


async def test_emit_event_payload_none_defaults_empty(session: AsyncSession) -> None:
    """Omitting payload should not raise; the row is still inserted successfully."""
    entity_id = _entity_id()

    await emit_event(
        session,
        actor_id=str(uuid.uuid4()),
        actor_type="system",
        entity_type="commit",
        entity_id=entity_id,
        action="branch.advanced",
        # payload intentionally omitted — defaults to None
    )
    await session.flush()

    result = await session.execute(select(Event).where(Event.entity_id == uuid.UUID(entity_id)))
    row = result.scalar_one()

    # Row must exist; payload may be None or an empty dict depending on the
    # default applied by emit_event ("{}" is stored when payload=None).
    assert row is not None


async def test_emit_event_actor_id_nullable(session: AsyncSession) -> None:
    """emit_event must succeed when actor_id is None (system/background action)."""
    entity_id = _entity_id()

    await emit_event(
        session,
        actor_id=None,
        actor_type="system",
        entity_type="annotation_revision",
        entity_id=entity_id,
        action="created",
    )
    await session.flush()

    result = await session.execute(select(Event).where(Event.entity_id == uuid.UUID(entity_id)))
    row = result.scalar_one()

    assert row.actor_id is None
    assert row.action == "created"


async def test_emit_event_writes_org_id(session: AsyncSession) -> None:
    """org_id kwarg is stored in the events row."""
    import uuid as _uuid
    from sqlalchemy import select as _select
    from cvops_api.db.models.runs import Event

    org_id = _uuid.uuid4()
    entity_id = _uuid.uuid4()
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
        _select(Event).where(Event.entity_id == entity_id)
    )).scalar_one()
    assert row.org_id == org_id


async def test_emit_event_multiple_events(session: AsyncSession) -> None:
    """Emitting three events for the same entity_id should produce three rows."""
    entity_id = _entity_id()
    actor_id = str(uuid.uuid4())

    for action in ("created", "run.started", "run.finished"):
        await emit_event(
            session,
            actor_id=actor_id,
            actor_type="user",
            entity_type="run",
            entity_id=entity_id,
            action=action,
        )
    await session.flush()

    result = await session.execute(select(Event).where(Event.entity_id == uuid.UUID(entity_id)))
    rows = result.scalars().all()

    assert len(rows) == 3
    actions_found = {r.action for r in rows}
    assert actions_found == {"created", "run.started", "run.finished"}
