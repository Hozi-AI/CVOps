"""Coordinator tests — advance_workflow on the streams model.

testcontainers Postgres (the `session` fixture) + fakeredis (`fake_redis`). The
`echo_step` fixture registers a trivial `test.echo` step so we exercise the DAG
plumbing without ffmpeg/S3.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from cvops_api.db.models.runs import Run
from cvops_api.db.models.auth import Org
from cvops_api.db.models.projects import Project
from cvops_api.db.models.workflows import Workflow
from cvops_api.engine.coordinator import advance_workflow
from tests.conftest import EchoStep

STREAM = "preprocessing"

_SINGLE_STEP_DEF = {
    "steps": [
        {
            "id": "s1",
            "type": "test.echo",
            "config": {},
            "inputs": {"src": "$run.params.source_id"},
        }
    ],
    "edges": [],
}


async def _seed_parent(session, definition: dict) -> tuple[Run, str]:
    """Insert org/project/workflow + a pending parent workflow run."""
    suffix = uuid.uuid4().hex[:8]
    org = Org(name=f"org-{suffix}")
    session.add(org)
    await session.flush()
    project = Project(org_id=org.id, name=f"proj-{suffix}")
    session.add(project)
    await session.flush()
    wf = Workflow(project_id=project.id, name=f"wf-{suffix}", definition=definition)
    session.add(wf)
    await session.flush()
    source_id = str(uuid.uuid4())
    parent = Run(
        project_id=project.id,
        workflow_id=wf.id,
        kind="workflow",
        status="pending",
        input_refs={"params": {"source_id": source_id}},
        output_refs={},
        config={},
    )
    session.add(parent)
    await session.commit()
    return parent, source_id


async def test_advance_creates_pending_child_and_enqueues(session, fake_redis, echo_step) -> None:
    parent, source_id = await _seed_parent(session, _SINGLE_STEP_DEF)
    actor = uuid.uuid4()

    await advance_workflow(session, parent.id, actor)

    # One child step run, pending, with frozen resolved inputs.
    children = (
        (await session.execute(select(Run).where(Run.parent_run_id == parent.id))).scalars().all()
    )
    assert len(children) == 1
    child = children[0]
    assert child.kind == "step"
    assert child.step_type == "test.echo"
    assert child.status == "pending"
    assert child.input_refs == {"src": source_id}  # resolved + frozen at create
    assert "_idem_key" in child.config

    # Parent not finalized — the step hasn't run yet.
    await session.refresh(parent)
    assert parent.status == "pending"

    # Exactly one thin doorbell message on the preprocessing stream.
    assert await fake_redis.xlen(STREAM) == 1
    entries = await fake_redis.xrange(STREAM)
    _msg_id, fields = entries[0]
    assert fields == {
        "job_id": str(child.id),
        "step_type": "test.echo",
        "queue": STREAM,
    }


async def test_advance_finalizes_parent_when_step_succeeds(session, fake_redis, echo_step) -> None:
    parent, _ = await _seed_parent(session, _SINGLE_STEP_DEF)
    actor = uuid.uuid4()
    await advance_workflow(session, parent.id, actor)

    child = (
        (await session.execute(select(Run).where(Run.parent_run_id == parent.id))).scalars().one()
    )
    # Simulate the worker finishing the step.
    child.status = "succeeded"
    child.output_refs = {"echoed": {"src": "x"}}
    await session.commit()

    await advance_workflow(session, parent.id, actor)

    await session.refresh(parent)
    assert parent.status == "succeeded"
    assert parent.finished_at is not None
    # No new enqueue: the only step is already done.
    assert await fake_redis.xlen(STREAM) == 1


async def test_advance_reuses_prior_succeeded_run(session, fake_redis, echo_step) -> None:
    parent, source_id = await _seed_parent(session, _SINGLE_STEP_DEF)
    actor = uuid.uuid4()

    # Pre-create a succeeded run whose idem key matches what s1 will compute.
    idem_key = EchoStep().idempotency_key({}, {"src": source_id})
    prior = Run(
        project_id=parent.project_id,
        kind="step",
        step_type="test.echo",
        status="succeeded",
        input_refs={"src": source_id},
        output_refs={"echoed": "from-prior"},
        config={"_idem_key": idem_key},
    )
    session.add(prior)
    await session.commit()

    await advance_workflow(session, parent.id, actor)

    child = (
        (await session.execute(select(Run).where(Run.parent_run_id == parent.id))).scalars().one()
    )
    # Reused: child is born succeeded with the prior outputs, nothing enqueued.
    assert child.status == "succeeded"
    assert child.output_refs == {"echoed": "from-prior"}
    assert await fake_redis.xlen(STREAM) == 0

    await session.refresh(parent)
    assert parent.status == "succeeded"


async def test_advance_reuses_when_multiple_prior_succeeded_runs(
    session, fake_redis, echo_step
) -> None:
    """A deterministic step run to success more than once leaves several rows
    sharing one idem key. advance must not choke (regression: scalar_one_or_none
    raised MultipleResultsFound) — it reuses the most recent one."""
    from datetime import UTC, datetime, timedelta

    parent, source_id = await _seed_parent(session, _SINGLE_STEP_DEF)
    actor = uuid.uuid4()

    idem_key = EchoStep().idempotency_key({}, {"src": source_id})
    base = datetime.now(UTC)
    for i, out in enumerate(["older", "newer"]):
        session.add(
            Run(
                project_id=parent.project_id,
                kind="step",
                step_type="test.echo",
                status="succeeded",
                input_refs={"src": source_id},
                output_refs={"echoed": out},
                config={"_idem_key": idem_key},
                finished_at=base + timedelta(minutes=i),
            )
        )
    await session.commit()

    await advance_workflow(session, parent.id, actor)

    child = (
        (await session.execute(select(Run).where(Run.parent_run_id == parent.id))).scalars().one()
    )
    # Most recent prior (by finished_at) wins; nothing enqueued.
    assert child.status == "succeeded"
    assert child.output_refs == {"echoed": "newer"}
    assert await fake_redis.xlen(STREAM) == 0


async def test_advance_adhoc_run_uses_inline_definition(session, fake_redis, echo_step) -> None:
    """An ad-hoc run (workflow_id=None) sources its DAG from config.definition
    rather than a saved Workflow, and advances exactly like one."""
    suffix = uuid.uuid4().hex[:8]
    org = Org(name=f"org-{suffix}")
    session.add(org)
    await session.flush()
    project = Project(org_id=org.id, name=f"proj-{suffix}")
    session.add(project)
    await session.flush()
    source_id = str(uuid.uuid4())
    parent = Run(
        project_id=project.id,
        workflow_id=None,
        kind="workflow",
        status="pending",
        input_refs={"params": {"source_id": source_id}},
        output_refs={},
        config={"definition": _SINGLE_STEP_DEF},
    )
    session.add(parent)
    await session.commit()
    actor = uuid.uuid4()

    await advance_workflow(session, parent.id, actor)

    child = (
        (await session.execute(select(Run).where(Run.parent_run_id == parent.id))).scalars().one()
    )
    assert child.workflow_id is None
    assert child.step_type == "test.echo"
    assert child.status == "pending"
    assert child.input_refs == {"src": source_id}
    assert await fake_redis.xlen(STREAM) == 1
