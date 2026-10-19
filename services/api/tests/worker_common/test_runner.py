"""Tests for ``cvops_worker_common.runner.run_job`` — the per-job lifecycle.

``run_job`` opens its own session via the module-level ``async_session_factory``
(patched to the test DB by ``worker_db``), claims the run with the
status='pending' + FOR UPDATE SKIP LOCKED guard, and hands it to the engine's
``process_step``:

  pending → running → succeeded | failed | waiting (gate)

On success ``process_step`` advances the workflow in-process (#182): it enqueues
the next DAG steps through ``get_redis()`` (``fake_redis``) or finalises the
parent; on failure it fails the parent too. ``coordinator.get_storage`` is
patched out because the test steps never use storage and the real backend
reaches for Garage at construction time.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import patch

from sqlalchemy import select

from cvops_api.core.registry import registry
from cvops_api.db.models.auth import Org
from cvops_api.db.models.projects import Project
from cvops_api.db.models.runs import Event, Run
from cvops_api.db.models.workflows import Workflow
from cvops_api.engine import coordinator
from cvops_api.engine.step import GateException, Step

from cvops_worker_common import runner

from .conftest import seed_parent_and_child


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


class _TrainingQueueStep(Step):
    """Stands in for step.train / step.auto_label: routed to the training queue."""

    type_key = "test.training_q"
    queue = "training"
    config_schema: dict[str, Any] = {"type": "object"}

    async def run(self, ctx, config, inputs):  # type: ignore[no-untyped-def]
        return {"model": "m-1"}


def _no_storage():  # type: ignore[no-untyped-def]
    return patch.object(coordinator, "get_storage", lambda: None)


async def _seed_two_step_dag(session, first_type: str) -> tuple[Run, Run]:  # type: ignore[no-untyped-def]
    """Workflow ``s1 → s2`` (s2 = test.echo), a pending parent and s1's pending child.

    s2 gets a unique input so the coordinator's idempotency reuse can never
    match an echo run left over from another test (which would create s2
    already-succeeded and skip the enqueue this test asserts on)."""
    suffix = uuid.uuid4().hex[:8]
    org = Org(name=f"org-{suffix}")
    session.add(org)
    await session.flush()
    project = Project(org_id=org.id, name=f"proj-{suffix}")
    session.add(project)
    await session.flush()
    definition = {
        "steps": [
            {"id": "s1", "type": first_type, "config": {}, "inputs": {}},
            {"id": "s2", "type": "test.echo", "config": {}, "inputs": {"nonce": suffix}},
        ],
        "edges": [{"source": "s1", "target": "s2"}],
    }
    wf = Workflow(project_id=project.id, name=f"wf-{suffix}", definition=definition)
    session.add(wf)
    await session.flush()
    parent = Run(
        project_id=project.id,
        workflow_id=wf.id,
        kind="workflow",
        status="running",
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
        step_type=first_type,
        status="pending",
        input_refs={},
        output_refs={},
        config={"_idem_key": "k-" + suffix},
    )
    session.add(child)
    await session.commit()
    return parent, child


# ── success → advance (#182) ──────────────────────────────────────────────────


async def test_run_job_success_marks_succeeded(session, fake_redis, echo_step, worker_db) -> None:
    _parent, child = await seed_parent_and_child(session, "test.echo", with_parent=False)

    with _no_storage():
        await runner.run_job(str(child.id), "test.echo")

    await session.refresh(child)
    assert child.status == "succeeded"
    assert child.output_refs == {"echoed": {}}
    assert child.started_at is not None and child.finished_at is not None

    actions = (
        (await session.execute(select(Event.action).where(Event.entity_id == child.id)))
        .scalars()
        .all()
    )
    assert "run.started" in actions
    assert "run.succeeded" in actions


async def test_run_job_success_finalises_the_parent(
    session, fake_redis, echo_step, worker_db
) -> None:
    """Last step of a one-step workflow: the parent reaches a terminal state
    instead of staying non-terminal forever (the #182 symptom)."""
    parent, child = await seed_parent_and_child(session, "test.echo", with_parent=True)

    with _no_storage():
        await runner.run_job(str(child.id), "test.echo")

    await session.refresh(child)
    await session.refresh(parent)
    assert child.status == "succeeded"
    assert parent.status == "succeeded"


async def test_training_queue_child_advances_the_dag(
    session, fake_redis, echo_step, worker_db
) -> None:
    """The #182 acceptance test: a step on the ``training`` queue succeeds and
    the next step is created and its doorbell XADDed — no HTTP callback."""
    registry.register(_TrainingQueueStep())
    try:
        parent, s1 = await _seed_two_step_dag(session, "test.training_q")

        with _no_storage():
            await runner.run_job(str(s1.id), "test.training_q")

        await session.refresh(s1)
        assert s1.status == "succeeded"
        assert s1.output_refs == {"model": "m-1"}

        s2 = (
            await session.execute(
                select(Run).where(Run.parent_run_id == parent.id, Run.step_id == "s2")
            )
        ).scalar_one()
        assert s2.status == "pending"

        # s2 (test.echo) routes to the default preprocessing queue.
        msgs = await fake_redis.xrange("preprocessing")
        assert any(f.get("job_id") == str(s2.id) for _id, f in msgs)

        # Parent is still in flight — s2 hasn't run yet.
        await session.refresh(parent)
        assert parent.status not in {"succeeded", "failed"}
    finally:
        registry._store.pop("test.training_q", None)


# ── gate → waiting ────────────────────────────────────────────────────────────


async def test_run_job_gate_parks_waiting(session, fake_redis, worker_db) -> None:
    registry.register(_GateStep())
    try:
        parent, child = await seed_parent_and_child(session, "test.gate", with_parent=True)
        with _no_storage():
            await runner.run_job(str(child.id), "test.gate")

        await session.refresh(child)
        assert child.status == "waiting"
        assert child.output_refs == {"gate_data": {"reason": "needs review"}}
        # A gate does not advance; resume comes through the gate-resolve endpoint.
        await session.refresh(parent)
        assert parent.status == "pending"
    finally:
        registry._store.pop("test.gate", None)


# ── exception → failed (child and parent) ─────────────────────────────────────


async def test_run_job_failure_fails_child_and_parent(session, fake_redis, worker_db) -> None:
    """#182's second half: a failing step used to fail only the child, leaving
    the parent non-terminal forever."""
    registry.register(_FailStep())
    try:
        parent, child = await seed_parent_and_child(session, "test.fail", with_parent=True)
        with _no_storage():
            await runner.run_job(str(child.id), "test.fail")

        await session.refresh(child)
        await session.refresh(parent)
        assert child.status == "failed"
        assert "boom" in (child.error or "")
        assert child.finished_at is not None
        assert parent.status == "failed"
    finally:
        registry._store.pop("test.fail", None)


async def test_run_job_unknown_step_type_fails_child_and_parent(
    session, fake_redis, worker_db
) -> None:
    """The claimed row's step_type decides what runs (Postgres is the
    authority); an unregistered one fails the child and its parent rather than
    escaping."""
    parent, child = await seed_parent_and_child(session, "test.no_such_step", with_parent=True)

    with _no_storage():
        await runner.run_job(str(child.id), "test.no_such_step")

    await session.refresh(child)
    await session.refresh(parent)
    assert child.status == "failed"
    assert "test.no_such_step" in (child.error or "")
    assert parent.status == "failed"


# ── claim guard ───────────────────────────────────────────────────────────────


async def test_run_job_skips_non_pending(session, fake_redis, echo_step, worker_db) -> None:
    """Only a pending row is claimed; a running/terminal run is left untouched,
    so redelivery of an in-flight job is a no-op."""
    _parent, child = await seed_parent_and_child(
        session, "test.echo", child_status="running", with_parent=False
    )

    with _no_storage():
        await runner.run_job(str(child.id), "test.echo")

    await session.refresh(child)
    assert child.status == "running"  # untouched


async def test_run_job_ignores_invalid_job_id(session, fake_redis, worker_db) -> None:
    """A malformed doorbell (non-UUID job_id) is dropped, not raised."""
    await runner.run_job("not-a-uuid", "test.echo")  # should simply return
