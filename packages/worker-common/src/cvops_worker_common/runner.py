"""
JobRunner — picks up a single job from the runs table and executes it.

Claims the run (FOR UPDATE SKIP LOCKED + status='pending' guard) and hands it to
the engine's ``process_step`` — the same lifecycle worker-preprocessing uses:

  pending → running → succeeded | failed | waiting (gate)

``process_step`` advances the workflow in-process on success (enqueues the next
DAG steps or finalises the parent) and fails the parent on a clean transaction
when the step raises. There is no HTTP callback: workers link ``cvops_api`` and
write to Postgres directly.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select

from cvops_api.db.models.runs import Run
from cvops_api.engine.coordinator import process_step

from cvops_worker_common.session import async_session_factory

logger = logging.getLogger(__name__)

# Worker-executed steps have no interactive user; events are attributed to the
# system sentinel (matches worker-preprocessing and worker_cvat.sync).
SYSTEM_ACTOR_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")


async def run_job(job_id: str, step_type: str) -> None:
    """Entry point called by ConsumerLoop for each message. Opens its own session.

    ``step_type`` is informational only: Postgres is the authority, so the step
    that runs is the claimed row's ``step_type``.
    """
    try:
        run_id = uuid.UUID(job_id)
    except ValueError:
        logger.warning("Dropping message with invalid job_id %r (%s)", job_id, step_type)
        return

    async with async_session_factory() as session:
        # At most one worker runs a given pending run; a redelivered or
        # already-finished job is a no-op.
        claimed = (
            await session.execute(
                select(Run)
                .where(Run.id == run_id, Run.status == "pending")
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if claimed is None:
            return
        await process_step(session, run_id, SYSTEM_ACTOR_ID)
