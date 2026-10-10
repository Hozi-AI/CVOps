"""process_step tests — the worker's per-job runner.

Uses the lightweight EchoStep (no ffmpeg/S3) plus local Gate/Fail steps to cover
the success → advance, gate → waiting, and exception → failed paths. get_storage
is patched out (EchoStep never touches storage; the real backend would try to
reach Garage at construction time).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from unittest.mock import patch

from cvops_api.core.registry import registry
from cvops_api.db.models.runs import Run, Event
from cvops_api.db.models.auth import Org
from cvops_api.db.models.projects import Project
from cvops_api.db.models.workflows import Workflow
from cvops_api.engine import coordinator
from cvops_api.engine.coordinator import process_step
from cvops_api.engine.step import Step, GateException

ACTOR = uuid.uuid4()

_DEF = {
    "steps": [{"id": "s1", "type": "test.echo", "config": {}, "inputs": {}}],
    "edges": [],
}


class _GateStep(Step):
    type_key = "test.gate"
    config_schema: dict[str, Any] = {"type": "object"}

    async def run(self, ctx, config, inputs):  # type: ignore[no-untyped-def]
        raise GateException({"reason": "needs review"})


class _FailStep(Step):
    type_key = "test.fail"
    config_schema: dict[str, Any] = {"type": "object"}

    async def run(self, ctx, config, inputs):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")


async def _seed_parent_and_child(session, step_type: str) -> tuple[Run, Run]:
    suffix = uuid.uuid4().hex[:8]
    org = Org(name=f"org-{suffix}")
    session.add(org)
    await session.flush()
    project = Project(org_id=org.id, name=f"proj-{suffix}")
    session.add(project)
    await session.flush()
    definition = {
        "steps": [{"id": "s1", "type": step_type, "config": {}, "inputs": {}}],
        "edges": [],
    }
    wf = Workflow(project_id=project.id, name=f"wf-{suffix}", definition=definition)
    session.add(wf)
    await session.flush()
    parent = Run(
        project_id=project.id,
        workflow_id=wf.id,
        kind="workflow",
        status="pending",
        input_refs={"params": {}},
        output_refs={},
        config={},
    )
    session.add(parent)
    await session.flush()
    child = Run(
        project_id=project.id,
        kind="step",
        parent_run_id=parent.id,
        workflow_id=wf.id,
        step_id="s1",
        step_type=step_type,
        status="pending",
        input_refs={},
        output_refs={},
        config={"_idem_key": "k-" + suffix},
    )
    session.add(child)
    await session.commit()
    return parent, child


async def test_process_step_success_then_advance(session, fake_redis, echo_step) -> None:
    parent, child = await _seed_parent_and_child(session, "test.echo")

    with patch.object(coordinator, "get_storage", lambda: None):
        await process_step(session, child.id, ACTOR)

    await session.refresh(child)
    assert child.status == "succeeded"
    assert child.output_refs == {"echoed": {}}
    assert child.started_at is not None and child.finished_at is not None

    # process_step calls advance_workflow on success → parent finalized.
    await session.refresh(parent)
    assert parent.status == "succeeded"

    # started + succeeded events recorded for the child.
    actions = (
        (await session.execute(select(Event.action).where(Event.entity_id == child.id)))
        .scalars()
        .all()
    )
    assert "run.started" in actions
    assert "run.succeeded" in actions


async def test_process_step_gate_parks_waiting(session, fake_redis) -> None:
    registry.register(_GateStep())
    try:
        parent, child = await _seed_parent_and_child(session, "test.gate")
        with patch.object(coordinator, "get_storage", lambda: None):
            await process_step(session, child.id, ACTOR)

        await session.refresh(child)
        assert child.status == "waiting"
        assert child.output_refs == {"gate_data": {"reason": "needs review"}}
        # Parent stays pending — a gate does not advance.
        await session.refresh(parent)
        assert parent.status == "pending"
    finally:
        registry._store.pop("test.gate", None)


async def test_process_step_failure_fails_parent(session, fake_redis) -> None:
    registry.register(_FailStep())
    try:
        parent, child = await _seed_parent_and_child(session, "test.fail")
        with patch.object(coordinator, "get_storage", lambda: None):
            await process_step(session, child.id, ACTOR)

        await session.refresh(child)
        assert child.status == "failed"
        assert "boom" in (child.error or "")
        await session.refresh(parent)
        assert parent.status == "failed"
    finally:
        registry._store.pop("test.fail", None)


async def test_process_step_skips_non_pending(session, fake_redis, echo_step) -> None:
    _parent, child = await _seed_parent_and_child(session, "test.echo")
    child.status = "running"
    await session.commit()

    with patch.object(coordinator, "get_storage", lambda: None):
        await process_step(session, child.id, ACTOR)

    # Untouched — process_step only runs pending rows.
    await session.refresh(child)
    assert child.status == "running"
