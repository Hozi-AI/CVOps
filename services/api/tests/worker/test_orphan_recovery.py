"""Unit test for the orphan-recovery query used by the preprocessing worker."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, UTC

from sqlalchemy import update

from cvops_api.db.models.runs import Run
from cvops_api.db.models.auth import Org
from cvops_api.db.models.projects import Project
from cvops_api.engine.coordinator import find_orphan_step_runs


async def _make_run(session, project_id, *, step_type: str, status: str) -> Run:
    run = Run(
        project_id=project_id,
        kind="step",
        step_type=step_type,
        status=status,
        input_refs={},
        output_refs={},
        config={},
    )
    session.add(run)
    await session.flush()
    return run


async def test_find_orphans_selects_only_stale_pending_in_set(session) -> None:
    org = Org(name=f"org-{uuid.uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    project = Project(org_id=org.id, name=f"proj-{uuid.uuid4().hex[:8]}")
    session.add(project)
    await session.flush()

    stale = await _make_run(session, project.id, step_type="step.extract_frames", status="pending")
    recent = await _make_run(session, project.id, step_type="step.extract_frames", status="pending")
    running = await _make_run(
        session, project.id, step_type="step.extract_frames", status="running"
    )
    other_queue = await _make_run(session, project.id, step_type="step.train", status="pending")

    # Backdate the stale row's created_at past the recovery threshold.
    await session.execute(
        update(Run)
        .where(Run.id == stale.id)
        .values(created_at=datetime.now(UTC) - timedelta(seconds=120))
    )
    await session.commit()

    orphans = await find_orphan_step_runs(
        session, {"step.extract_frames", "step.auto_label"}, min_age_seconds=30
    )
    ids = {rid for rid, _ in orphans}

    assert stale.id in ids
    assert recent.id not in ids  # too young
    assert running.id not in ids  # not pending
    assert other_queue.id not in ids  # step_type not in this worker's set


async def test_find_orphans_empty_set_returns_nothing(session) -> None:
    assert await find_orphan_step_runs(session, set()) == []
