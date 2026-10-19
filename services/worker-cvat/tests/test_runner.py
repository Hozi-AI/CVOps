"""Regression: the worker runner must attribute step-emitted events to a valid
UUID actor.

run_job built its StepContext with actor_id="service:worker". The moment a step
called ctx.emit_event(actor_id=ctx.actor_id, ...) — as human_review does on its
"labeling.pushed" event — that non-UUID string hit the events table's UUID
column and raised an asyncpg DataError, failing the run *after* the CVAT task was
created (so the gate never opened). The runner now uses a system-sentinel UUID.

DB-backed (testcontainers); shares the worker-cvat conftest. Requires Docker.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text

from cvops_api.core.registry import registry
from cvops_api.db.models.auth import Org
from cvops_api.db.models.projects import Project
from cvops_api.db.models.runs import Run
from cvops_api.engine import coordinator
from cvops_api.engine.step import GateException, Step

import cvops_worker_common.runner as runner


class _EmitGateStep(Step):
    """Emits an event attributed to ctx.actor_id, then parks at a gate — the
    exact shape that tripped the bug."""

    type_key = "test.emit_actor_gate"
    is_gate = True
    config_schema = {"type": "object"}

    async def run(self, ctx, config, inputs):  # noqa: ANN001
        await ctx.emit_event(
            actor_id=ctx.actor_id,
            actor_type="system",
            entity_type="labeling_job",
            entity_id=str(uuid.uuid4()),
            action="test.emitted",
        )
        raise GateException({"ok": True})


async def test_runner_attributes_events_to_a_valid_uuid_actor(session_factory, monkeypatch):
    registry.register(_EmitGateStep())
    monkeypatch.setattr(runner, "async_session_factory", session_factory)
    # The step doesn't touch storage; avoid get_storage()'s live S3 bucket check.
    # run_job delegates to the engine's process_step, which builds the context.
    monkeypatch.setattr(coordinator, "get_storage", lambda: object())
    try:
        async with session_factory() as s:
            org = Org(name=f"org-{uuid.uuid4().hex[:8]}")
            s.add(org)
            await s.flush()
            proj = Project(org_id=org.id, name=f"p-{uuid.uuid4().hex[:8]}")
            s.add(proj)
            await s.flush()
            run = Run(
                project_id=proj.id,
                kind="step",
                status="pending",
                step_type="test.emit_actor_gate",
                step_id="review_node",
            )
            s.add(run)
            await s.commit()
            run_id = str(run.id)

        # Would raise/fail the run with the old "service:worker" actor id.
        await runner.run_job(run_id, "test.emit_actor_gate")

        async with session_factory() as s:
            actor = (
                await s.execute(
                    text("SELECT actor_id FROM events WHERE action = 'test.emitted'")
                )
            ).scalar()
            assert actor is not None
            assert str(actor) == str(runner.SYSTEM_ACTOR_ID)  # a real UUID, not "service:worker"
            uuid.UUID(str(actor))  # parseable

            status = (
                await s.execute(
                    text("SELECT status FROM runs WHERE id = CAST(:r AS uuid)"),
                    {"r": run_id},
                )
            ).scalar()
            assert status == "waiting"  # gate parked, not failed
    finally:
        registry._store.pop("test.emit_actor_gate", None)
