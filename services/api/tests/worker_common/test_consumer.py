"""Tests for ``cvops_worker_common.consumer.ConsumerLoop``.

Covers the small pieces the infinite loop is built from, driven for a single
message / single recovery pass:

  * ``_ensure_group``    — consumer-group create idempotency (BUSYGROUP
                           swallowed; other ResponseError re-raised).
  * ack-always contract  — the loop body always XACKs a message even when the
                           handler raises; re-implemented inline here since the
                           dispatch lives inside ``_consume_loop``.
  * end-to-end claim+run — seed a pending Run, XADD a doorbell, drive one
                           claim+run iteration, assert succeeded + empty PEL.
  * claim guard          — only a ``pending`` Run is processed; re-delivery of
                           an already-terminal run is a no-op.
  * ``_recover_orphans`` — a stale pending run is re-enqueued onto the stream;
                           recent/running rows are not.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from redis.exceptions import ResponseError
from sqlalchemy import update

from cvops_api.db.models.runs import Run
from cvops_api.engine import coordinator

from cvops_worker_common import runner
from cvops_worker_common.consumer import ConsumerLoop

from .conftest import seed_parent_and_child


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_loop(stream: str = "preprocessing") -> ConsumerLoop:
    return ConsumerLoop(stream=stream, step_types=["test.echo"])


async def _dispatch_and_ack(
    loop: ConsumerLoop, fake_redis, msg_id: str, fields: dict[str, str]
) -> None:
    """Re-implementation of the per-message body of ``_consume_loop`` for one
    message: route to the handler, then ALWAYS ack in a finally block (the
    worker never auto-requeues; failures live in PG, not Redis)."""
    kind = fields.get("kind", "")
    job_id = fields.get("job_id", "")
    step_type = fields.get("step_type", "")
    try:
        if kind:
            if loop.sync_handler is not None:
                await loop.sync_handler(fields)
        else:
            await loop.handler(job_id, step_type)
    finally:
        await fake_redis.xack(loop.stream, loop.group, msg_id)


async def _pending_entries(fake_redis, stream: str, group: str) -> int:
    """Count not-yet-acked entries in the group's pending-entries list (PEL)."""
    info = await fake_redis.xpending(stream, group)
    return int(info["pending"]) if isinstance(info, dict) else int(info[0])


# ── _ensure_group idempotency ────────────────────────────────────────────────


async def test_ensure_group_creates_then_is_idempotent(fake_redis) -> None:
    loop = _make_loop()

    # First call creates the group + stream (mkstream=True).
    await loop._ensure_group(fake_redis)
    groups = await fake_redis.xinfo_groups(loop.stream)
    assert any(g["name"] == loop.group for g in groups)

    # Second call hits BUSYGROUP and must be swallowed (no raise, no dup group).
    await loop._ensure_group(fake_redis)
    groups = await fake_redis.xinfo_groups(loop.stream)
    assert sum(1 for g in groups if g["name"] == loop.group) == 1


async def test_ensure_group_reraises_non_busygroup(fake_redis) -> None:
    loop = _make_loop()

    async def _boom(*_a: Any, **_k: Any) -> None:
        raise ResponseError("ERR something else went wrong")

    with patch.object(fake_redis, "xgroup_create", _boom):
        with pytest.raises(ResponseError, match="something else"):
            await loop._ensure_group(fake_redis)


# ── end-to-end claim + run + ack ──────────────────────────────────────────────


async def test_dispatch_runs_step_and_acks(session, fake_redis, echo_step, worker_db) -> None:
    """Seed a pending run, XADD a doorbell, drive one dispatch iteration:
    the run advances to succeeded and the message is XACK'd (empty PEL).

    No parent_run_id, so there is no workflow to advance.
    """
    _parent, child = await seed_parent_and_child(session, "test.echo", with_parent=False)
    loop = _make_loop()

    await loop._ensure_group(fake_redis)
    await coordinator.enqueue_step(child.id, "test.echo", loop.stream)

    # Read the one message back out of the group, as the loop's XREADGROUP would.
    resp = await fake_redis.xreadgroup(
        loop.group, loop.consumer_name, streams={loop.stream: ">"}, count=10
    )
    (_stream, messages) = resp[0]
    assert len(messages) == 1
    msg_id, fields = messages[0]
    assert fields["job_id"] == str(child.id)

    with patch.object(coordinator, "get_storage", lambda: None):
        await _dispatch_and_ack(loop, fake_redis, msg_id, fields)

    # Run advanced through the runner.
    await session.refresh(child)
    assert child.status == "succeeded"
    assert child.output_refs == {"echoed": {}}
    assert child.started_at is not None and child.finished_at is not None

    # Message acked → empty pending-entries list.
    assert await _pending_entries(fake_redis, loop.stream, loop.group) == 0


async def test_dispatch_acks_even_when_handler_raises(
    session, fake_redis, echo_step, worker_db
) -> None:
    """If the handler raises, the message is STILL acked — the worker never
    auto-requeues; failures are recorded in PG, not redelivered by Redis."""
    _parent, child = await seed_parent_and_child(session, "test.echo", with_parent=False)
    loop = _make_loop()

    await loop._ensure_group(fake_redis)
    await coordinator.enqueue_step(child.id, "test.echo", loop.stream)
    resp = await fake_redis.xreadgroup(
        loop.group, loop.consumer_name, streams={loop.stream: ">"}, count=10
    )
    msg_id, fields = resp[0][1][0]

    async def _explode(_job_id: str, _step_type: str) -> None:
        raise RuntimeError("handler blew up")

    loop.handler = _explode
    with pytest.raises(RuntimeError, match="handler blew up"):
        await _dispatch_and_ack(loop, fake_redis, msg_id, fields)

    # Despite the exception, the message was acked in the finally block.
    assert await _pending_entries(fake_redis, loop.stream, loop.group) == 0


# ── claim guard (idempotent against re-enqueue / redelivery) ──────────────────


async def test_claim_skips_non_pending_run(session, fake_redis, echo_step, worker_db) -> None:
    """A run already in a terminal state is NOT re-processed — the
    status='pending' claim guard in ``run_job`` makes re-delivery a no-op."""
    _parent, child = await seed_parent_and_child(
        session, "test.echo", child_status="succeeded", with_parent=False
    )
    # Give it a sentinel output to prove run_job leaves it untouched.
    await session.execute(
        update(Run).where(Run.id == child.id).values(output_refs={"sentinel": True})
    )
    await session.commit()

    await runner.run_job(str(child.id), "test.echo")

    await session.refresh(child)
    assert child.status == "succeeded"
    assert child.output_refs == {"sentinel": True}  # step never ran


async def test_claim_unknown_run_is_noop(session, fake_redis, worker_db) -> None:
    """A job_id with no matching row claims nothing and does not raise."""
    await runner.run_job(str(uuid.uuid4()), "test.echo")  # should simply return


# ── orphan recovery (one pass) ────────────────────────────────────────────────


async def test_recover_orphans_reenqueues_stale_pending(
    session, fake_redis, echo_step, worker_db
) -> None:
    """A stale pending run for one of this worker's step types is re-enqueued
    onto the stream by a single ``_recover_orphans`` pass."""
    _parent, child = await seed_parent_and_child(session, "test.echo", with_parent=False)
    # Backdate past ORPHAN_PENDING_AGE_SECONDS (default 30) so it is orphaned.
    await session.execute(
        update(Run)
        .where(Run.id == child.id)
        .values(created_at=datetime.now(UTC) - timedelta(seconds=120))
    )
    await session.commit()

    loop = _make_loop()
    await loop._recover_orphans(fake_redis)

    # The doorbell for our run now sits on the stream.
    entries = await fake_redis.xrange(loop.stream)
    job_ids = {fields["job_id"] for _id, fields in entries}
    assert str(child.id) in job_ids


async def test_recover_orphans_ignores_recent_and_running(
    session, fake_redis, echo_step, worker_db
) -> None:
    """Recent pending and already-running rows are not re-enqueued."""
    _p1, recent = await seed_parent_and_child(session, "test.echo", with_parent=False)
    _p2, running = await seed_parent_and_child(
        session, "test.echo", child_status="running", with_parent=False
    )

    loop = _make_loop()
    await loop._recover_orphans(fake_redis)

    entries = await fake_redis.xrange(loop.stream)
    job_ids = {fields["job_id"] for _id, fields in entries}
    assert str(recent.id) not in job_ids  # too young
    assert str(running.id) not in job_ids  # not pending


async def test_recover_orphans_ignores_other_step_types(session, fake_redis, worker_db) -> None:
    """A stale pending run whose step_type is not in this worker's set is left
    alone — orphan recovery is scoped to the worker's own step types."""
    _parent, child = await seed_parent_and_child(session, "step.train", with_parent=False)
    await session.execute(
        update(Run)
        .where(Run.id == child.id)
        .values(created_at=datetime.now(UTC) - timedelta(seconds=120))
    )
    await session.commit()

    loop = _make_loop()  # step_types=["test.echo"], not "step.train"
    await loop._recover_orphans(fake_redis)

    entries = await fake_redis.xrange(loop.stream)
    job_ids = {fields["job_id"] for _id, fields in entries}
    assert str(child.id) not in job_ids


# ── global Redis for in-process advance (#182) ────────────────────────────────


async def test_run_forever_initialises_global_redis_for_advance(monkeypatch) -> None:
    """process_step → advance_workflow enqueues via cvops_api's get_redis(), so
    run_forever must initialise that client for the loop's lifetime (and close
    it after). Without it, a training step would succeed and then crash on
    "Redis client not initialized" instead of advancing the workflow."""
    from cvops_api.core import redis_client

    loop = _make_loop("training")
    seen: dict[str, object] = {}

    async def _noop(_redis: object) -> None:
        return None

    async def _consume(_redis: object) -> None:
        seen["client"] = redis_client.get_redis()  # raises if not initialised

    monkeypatch.setattr(loop, "_ensure_group", _noop)
    monkeypatch.setattr(loop, "_recover_orphans", _noop)
    monkeypatch.setattr(loop, "_consume_loop", _consume)

    await loop.run_forever()

    assert seen["client"] is not None
    with pytest.raises(RuntimeError, match="not initialized"):
        redis_client.get_redis()  # closed again on shutdown
