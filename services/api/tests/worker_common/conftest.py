"""Shared fixtures + seed helpers for the ``cvops_worker_common`` tests.

``cvops_worker_common`` is the generic Redis-Streams consumer/runner that the
preprocessing, cvat, and training workers all build on. It is importable in the
API venv (it depends on ``cvops-api``), so these tests live under the API suite
and reuse its ``session`` (testcontainers Postgres), ``fake_redis``
(fakeredis.aioredis), and ``echo_step`` fixtures from ``tests/conftest.py``.

We never run the infinite ``ConsumerLoop._consume_loop`` here. Instead we drive
the small internal pieces for a single message/iteration:

  * ``ConsumerLoop._ensure_group``    — group-create idempotency (BUSYGROUP).
  * ``cvops_worker_common.runner.run_job`` — the per-message handler: it claims
    the run (FOR UPDATE SKIP LOCKED + status='pending' guard) and hands it to
    the engine's ``process_step``, which runs, finalises and advances it.
  * the ack-always contract           — re-implemented inline the way
    ``ConsumerLoop._consume_loop`` does it (its handler dispatch is inlined in
    the loop body and cannot be imported directly).
  * ``ConsumerLoop._recover_orphans`` — one recovery pass against the test DB.

The runner and consumer both open their own session via the module-level
``async_session_factory`` (imported by name into each module), so seeded rows
must be COMMITTED before they run — they cannot see uncommitted state from the
test ``session`` fixture's transaction.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cvops_api.db.models.auth import Org
from cvops_api.db.models.projects import Project
from cvops_api.db.models.runs import Run
from cvops_api.db.models.workflows import Workflow

from cvops_worker_common import consumer, runner


@pytest.fixture
async def worker_db(postgres_url: str):  # type: ignore[no-untyped-def]
    """Point the worker's module-level ``async_session_factory`` at the test DB.

    Both ``runner`` and ``consumer`` import ``async_session_factory`` by name
    from ``cvops_worker_common.session``; that maker is bound to the *default*
    settings engine — not the testcontainers Postgres. Patch the name in both
    modules to a maker on the test URL so ``run_job`` and the orphan-recovery
    sessions read committed rows seeded by the ``session`` fixture. Yields the
    maker for tests that want a fresh session of their own.
    """
    engine = create_async_engine(postgres_url, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    with (
        patch.object(runner, "async_session_factory", factory),
        patch.object(consumer, "async_session_factory", factory),
    ):
        yield factory
    await engine.dispose()


async def seed_parent_and_child(
    session,
    step_type: str,
    *,
    child_status: str = "pending",
    with_parent: bool = True,
) -> tuple[Run | None, Run]:
    """Seed an org/project/workflow + an optional pending parent and one child.

    Committed (not just flushed) so the worker's own session can see the rows.
    ``with_parent=False`` leaves ``parent_run_id`` NULL, so a successful run has
    no workflow to advance.
    """
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

    parent: Run | None = None
    parent_id = None
    if with_parent:
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
        parent_id = parent.id

    child = Run(
        project_id=project.id,
        kind="step",
        parent_run_id=parent_id,
        workflow_id=wf.id,
        step_id="s1",
        step_type=step_type,
        status=child_status,
        input_refs={},
        output_refs={},
        config={"_idem_key": "k-" + suffix},
    )
    session.add(child)
    await session.commit()
    return parent, child
