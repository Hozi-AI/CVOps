import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.db.models.runs import Event, Run
from tests.db.conftest import make_project, make_run


# ---------------------------------------------------------------------------
# Run tests
# ---------------------------------------------------------------------------


async def test_run_create(session: AsyncSession):
    project = await make_project(session)
    run = Run(
        project_id=project.id,
        kind="workflow",
        input_refs={},
        output_refs={},
        config={},
    )
    session.add(run)
    await session.flush()

    assert run.id is not None
    assert run.kind == "workflow"
    assert run.project_id == project.id


async def test_run_status_default(session: AsyncSession):
    project = await make_project(session)
    run = Run(
        project_id=project.id,
        kind="workflow",
        input_refs={},
        output_refs={},
        config={},
    )
    session.add(run)
    await session.flush()
    await session.refresh(run)

    assert run.status == "pending"


async def test_run_attempt_default(session: AsyncSession):
    project = await make_project(session)
    run = Run(
        project_id=project.id,
        kind="workflow",
        input_refs={},
        output_refs={},
        config={},
    )
    session.add(run)
    await session.flush()
    await session.refresh(run)

    assert run.attempt == 1


async def test_run_input_refs_default(session: AsyncSession):
    project = await make_project(session)
    run = Run(
        project_id=project.id,
        kind="workflow",
        output_refs={},
        config={},
    )
    session.add(run)
    await session.flush()
    await session.refresh(run)

    assert run.input_refs == {}


async def test_run_self_referential_fk(session: AsyncSession):
    project = await make_project(session)
    parent_run = await make_run(session, project_id=project.id, kind="workflow")

    child_run = Run(
        project_id=project.id,
        kind="step",
        parent_run_id=parent_run.id,
        input_refs={},
        output_refs={},
        config={},
    )
    session.add(child_run)
    await session.flush()

    assert child_run.parent_run_id == parent_run.id


async def test_run_status_transition(session: AsyncSession):
    run = await make_run(session)
    await session.flush()

    run.status = "running"
    await session.flush()
    await session.refresh(run)

    assert run.status == "running"


async def test_run_jsonb_fields(session: AsyncSession):
    project = await make_project(session)
    run = Run(
        project_id=project.id,
        kind="step",
        input_refs={"source_id": "abc"},
        output_refs={},
        config={},
    )
    session.add(run)
    await session.flush()
    await session.refresh(run)

    assert run.input_refs["source_id"] == "abc"


async def test_run_project_fk(session: AsyncSession):
    fake_project_id = uuid.uuid4()
    run = Run(
        project_id=fake_project_id,
        kind="workflow",
        input_refs={},
        output_refs={},
        config={},
    )
    session.add(run)

    with pytest.raises(IntegrityError):
        await session.flush()

    await session.rollback()


# ---------------------------------------------------------------------------
# Event tests
# ---------------------------------------------------------------------------


async def test_event_create(session: AsyncSession):
    event = Event(
        entity_type="run",
        entity_id=uuid.uuid4(),
        action="started",
    )
    session.add(event)
    await session.flush()

    assert event.id is not None
    assert event.entity_type == "run"
    assert event.action == "started"


async def test_event_no_updated_at(session: AsyncSession):
    event = Event(
        entity_type="run",
        entity_id=uuid.uuid4(),
        action="created",
    )
    session.add(event)
    await session.flush()

    assert not hasattr(event, "updated_at")


async def test_event_no_deleted_at(session: AsyncSession):
    event = Event(
        entity_type="run",
        entity_id=uuid.uuid4(),
        action="created",
    )
    session.add(event)
    await session.flush()

    assert not hasattr(event, "deleted_at")


async def test_event_payload_jsonb(session: AsyncSession):
    entity_id = uuid.uuid4()
    event = Event(
        entity_type="run",
        entity_id=entity_id,
        action="updated",
        payload={"key": "val"},
    )
    session.add(event)
    await session.flush()

    result = await session.execute(select(Event).where(Event.id == event.id))
    fetched = result.scalar_one()

    assert fetched.payload["key"] == "val"


async def test_event_actor_id_nullable(session: AsyncSession):
    event = Event(
        actor_id=None,
        entity_type="run",
        entity_id=uuid.uuid4(),
        action="gc",
    )
    session.add(event)
    await session.flush()

    assert event.actor_id is None


async def test_event_has_org_id(session: AsyncSession) -> None:
    """org_id column exists, is writable, and survives a flush/refresh."""
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
