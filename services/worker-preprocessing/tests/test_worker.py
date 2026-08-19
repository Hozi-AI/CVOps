"""Unit tests for the preprocessing worker's consume/dispatch logic.

Coverage focus (no Postgres — see conftest):
  * ``_my_step_types`` — which step types route to this worker's stream.
  * ``_ensure_group`` — idempotent consumer-group creation.
  * ``_claim_and_run`` — error propagation through the claim wrapper.
  * ``_consume_loop`` — reads doorbell messages off a real (fake) stream,
    dispatches each, and ACKs even when the handler raises (failures are
    recorded in PG, never auto-requeued).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

import cvops_worker.worker as worker


# ── _my_step_types ───────────────────────────────────────────────────────────


def test_my_step_types_includes_default_queue_steps(stub_registry):
    # Empty queue → DEFAULT_QUEUE ("preprocessing", which STREAM defaults to).
    from tests.conftest import FakeReg

    stub_registry(
        FakeReg("step.extract_frames", queue=""),
        FakeReg("step.commit_dataset", queue="preprocessing"),
        FakeReg("step.train", queue="training"),
        FakeReg("step.human_review", queue="cvat"),
    )
    assert worker._my_step_types() == {"step.extract_frames", "step.commit_dataset"}


def test_my_step_types_empty_when_nothing_routes_here(stub_registry):
    from tests.conftest import FakeReg

    stub_registry(FakeReg("step.train", queue="training"))
    assert worker._my_step_types() == set()


def test_my_step_types_empty_registry(stub_registry):
    stub_registry()
    assert worker._my_step_types() == set()


# ── _ensure_group ────────────────────────────────────────────────────────────


async def test_ensure_group_creates_stream_and_group(fake_redis):
    await worker._ensure_group()
    groups = await fake_redis.xinfo_groups(worker.STREAM)
    assert any(g["name"] == worker.GROUP for g in groups)


async def test_ensure_group_is_idempotent(fake_redis):
    # BUSYGROUP on the second call must be swallowed, not raised.
    await worker._ensure_group()
    await worker._ensure_group()
    groups = await fake_redis.xinfo_groups(worker.STREAM)
    assert sum(g["name"] == worker.GROUP for g in groups) == 1


async def test_ensure_group_reraises_non_busygroup(fake_redis, monkeypatch):
    class Boom(Exception):
        pass

    async def _xgroup_create(*a, **k):
        raise Boom("something else entirely")

    monkeypatch.setattr(fake_redis, "xgroup_create", _xgroup_create)
    with pytest.raises(Boom):
        await worker._ensure_group()


# ── _claim_and_run ───────────────────────────────────────────────────────────


async def test_claim_and_run_noop_when_not_pending(monkeypatch):
    """A re-enqueued / already-finished job claims nothing → process_step skipped."""
    job_id = str(uuid.uuid4())

    # The select returns no pending row.
    class _Result:
        def scalar_one_or_none(self):
            return None

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, *a, **k):
            return _Result()

    process_called = False

    async def _process_step(*a, **k):
        nonlocal process_called
        process_called = True

    monkeypatch.setattr(worker, "async_session_factory", lambda: _Session())
    monkeypatch.setattr(worker, "process_step", _process_step)

    await worker._claim_and_run(job_id)
    assert process_called is False


async def test_claim_and_run_invokes_process_step_when_claimed(monkeypatch):
    job_id = str(uuid.uuid4())
    claimed_obj = object()

    class _Result:
        def scalar_one_or_none(self):
            return claimed_obj

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, *a, **k):
            return _Result()

    seen = {}

    async def _process_step(session, run_id, actor_id):
        seen["run_id"] = run_id
        seen["actor_id"] = actor_id

    monkeypatch.setattr(worker, "async_session_factory", lambda: _Session())
    monkeypatch.setattr(worker, "process_step", _process_step)

    await worker._claim_and_run(job_id)
    assert seen["run_id"] == uuid.UUID(job_id)
    assert seen["actor_id"] == worker.SYSTEM_ACTOR_ID


# ── _consume_loop ────────────────────────────────────────────────────────────


async def test_consume_loop_dispatches_and_acks(fake_redis, monkeypatch):
    handled: list[str] = []

    async def _claim(job_id: str) -> None:
        handled.append(job_id)

    monkeypatch.setattr(worker, "_claim_and_run", _claim)
    # Keep the blocking read short so the loop notices stop promptly.
    monkeypatch.setattr(worker, "BLOCK_MS", 50)

    await worker._ensure_group()
    job_id = str(uuid.uuid4())
    await fake_redis.xadd(worker.STREAM, {"job_id": job_id, "step_type": "step.x"})

    stop = asyncio.Event()
    sem = asyncio.Semaphore(worker.CONCURRENCY)
    task = asyncio.create_task(worker._consume_loop(stop, sem))

    # Wait for the handler to observe the message.
    for _ in range(100):
        if handled:
            break
        await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, timeout=5)

    assert handled == [job_id]
    # Message was acked → no pending entries left for this consumer group.
    pending = await fake_redis.xpending(worker.STREAM, worker.GROUP)
    assert pending["pending"] == 0


async def test_consume_loop_acks_even_when_handler_raises(fake_redis, monkeypatch):
    async def _claim(job_id: str) -> None:
        raise RuntimeError("step blew up")

    monkeypatch.setattr(worker, "_claim_and_run", _claim)
    monkeypatch.setattr(worker, "BLOCK_MS", 50)

    await worker._ensure_group()
    await fake_redis.xadd(worker.STREAM, {"job_id": str(uuid.uuid4())})

    stop = asyncio.Event()
    sem = asyncio.Semaphore(worker.CONCURRENCY)
    task = asyncio.create_task(worker._consume_loop(stop, sem))

    # Give it time to read + (fail to) handle + ack.
    for _ in range(100):
        pending = await fake_redis.xpending(worker.STREAM, worker.GROUP)
        if pending["pending"] == 0 and await _delivered(fake_redis):
            break
        await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, timeout=5)

    # Even though the handler raised, the message was acked (no auto-requeue).
    pending = await fake_redis.xpending(worker.STREAM, worker.GROUP)
    assert pending["pending"] == 0


# ── retry / DLQ ─────────────────────────────────────────────────────────────


async def test_poison_pill_goes_to_dlq(fake_redis, monkeypatch):
    """A message missing job_id is sent to the DLQ without ever calling _claim_and_run."""
    monkeypatch.setattr(worker, "BLOCK_MS", 50)
    claim_called = False

    async def _claim(job_id: str) -> None:
        nonlocal claim_called
        claim_called = True

    monkeypatch.setattr(worker, "_claim_and_run", _claim)

    await worker._ensure_group()
    await fake_redis.xadd(worker.STREAM, {"step_type": "step.x"})  # no job_id

    stop = asyncio.Event()
    sem = asyncio.Semaphore(worker.CONCURRENCY)
    task = asyncio.create_task(worker._consume_loop(stop, sem))

    for _ in range(100):
        dlq_msgs = await fake_redis.xrange(worker.DLQ_STREAM)
        if dlq_msgs:
            break
        await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, timeout=5)

    assert not claim_called
    dlq_msgs = await fake_redis.xrange(worker.DLQ_STREAM)
    assert len(dlq_msgs) == 1
    assert dlq_msgs[0][1]["dlq_reason"] == "missing job_id"


async def test_transient_failure_requeues_with_retry_counter(fake_redis, monkeypatch):
    """A failing job is re-enqueued and its retry counter incremented."""
    monkeypatch.setattr(worker, "BLOCK_MS", 50)
    monkeypatch.setattr(worker, "MAX_RETRIES", 3)
    monkeypatch.setattr(worker, "_RETRY_DELAYS", (0.0, 0.0, 0.0))

    async def _claim(job_id: str) -> None:
        raise RuntimeError("transient error")

    monkeypatch.setattr(worker, "_claim_and_run", _claim)

    await worker._ensure_group()
    job_id = str(uuid.uuid4())
    await fake_redis.xadd(worker.STREAM, {"job_id": job_id, "step_type": "step.x"})

    stop = asyncio.Event()
    sem = asyncio.Semaphore(worker.CONCURRENCY)
    task = asyncio.create_task(worker._consume_loop(stop, sem))

    # Wait for the retry counter to be set.
    retry_key = f"{worker._RETRY_KEY_PREFIX}{job_id}"
    for _ in range(100):
        val = await fake_redis.get(retry_key)
        if val is not None:
            break
        await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, timeout=5)

    assert int(await fake_redis.get(retry_key) or 0) >= 1
    # Original message was acked; a new one was re-enqueued.
    pending = await fake_redis.xpending(worker.STREAM, worker.GROUP)
    assert pending["pending"] == 0


async def test_max_retries_exceeded_sends_to_dlq(fake_redis, monkeypatch):
    """After MAX_RETRIES failures the job lands in the DLQ, not re-enqueued."""
    monkeypatch.setattr(worker, "BLOCK_MS", 50)
    monkeypatch.setattr(worker, "MAX_RETRIES", 2)
    monkeypatch.setattr(worker, "_RETRY_DELAYS", (0.0, 0.0))

    async def _claim(job_id: str) -> None:
        raise RuntimeError("always fails")

    monkeypatch.setattr(worker, "_claim_and_run", _claim)

    await worker._ensure_group()
    job_id = str(uuid.uuid4())
    # Pre-seed the counter so this attempt is already at the limit.
    retry_key = f"{worker._RETRY_KEY_PREFIX}{job_id}"
    await fake_redis.set(retry_key, worker.MAX_RETRIES)

    await fake_redis.xadd(worker.STREAM, {"job_id": job_id, "step_type": "step.x"})

    stop = asyncio.Event()
    sem = asyncio.Semaphore(worker.CONCURRENCY)
    task = asyncio.create_task(worker._consume_loop(stop, sem))

    for _ in range(100):
        dlq_msgs = await fake_redis.xrange(worker.DLQ_STREAM)
        if dlq_msgs:
            break
        await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, timeout=5)

    dlq_msgs = await fake_redis.xrange(worker.DLQ_STREAM)
    assert len(dlq_msgs) == 1
    assert dlq_msgs[0][1]["job_id"] == job_id
    # Retry key was cleaned up.
    assert await fake_redis.get(retry_key) is None


async def test_success_clears_retry_counter(fake_redis, monkeypatch):
    """A successful run deletes the retry key so future transient failures start fresh."""
    monkeypatch.setattr(worker, "BLOCK_MS", 50)

    async def _claim(job_id: str) -> None:
        return None

    monkeypatch.setattr(worker, "_claim_and_run", _claim)

    await worker._ensure_group()
    job_id = str(uuid.uuid4())
    retry_key = f"{worker._RETRY_KEY_PREFIX}{job_id}"
    await fake_redis.set(retry_key, "2")  # simulate two prior failures

    await fake_redis.xadd(worker.STREAM, {"job_id": job_id, "step_type": "step.x"})

    stop = asyncio.Event()
    sem = asyncio.Semaphore(worker.CONCURRENCY)
    task = asyncio.create_task(worker._consume_loop(stop, sem))

    for _ in range(100):
        pending = await fake_redis.xpending(worker.STREAM, worker.GROUP)
        if pending["pending"] == 0 and await _delivered(fake_redis):
            break
        await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, timeout=5)

    assert await fake_redis.get(retry_key) is None


async def _delivered(fake_redis) -> bool:
    info = await fake_redis.xinfo_groups(worker.STREAM)
    return any(g["name"] == worker.GROUP and g["last-delivered-id"] != "0-0" for g in info)


async def test_consume_loop_releases_semaphore_per_message(fake_redis, monkeypatch):
    """The semaphore is acquired before dispatch and released after ack, so a
    burst of messages doesn't permanently drain it."""
    async def _claim(job_id: str) -> None:
        return None

    monkeypatch.setattr(worker, "_claim_and_run", _claim)
    monkeypatch.setattr(worker, "BLOCK_MS", 50)

    await worker._ensure_group()
    for _ in range(3):
        await fake_redis.xadd(worker.STREAM, {"job_id": str(uuid.uuid4())})

    stop = asyncio.Event()
    sem = asyncio.Semaphore(2)  # fewer permits than messages
    task = asyncio.create_task(worker._consume_loop(stop, sem))

    for _ in range(150):
        pending = await fake_redis.xpending(worker.STREAM, worker.GROUP)
        if pending["pending"] == 0 and await _delivered(fake_redis):
            break
        await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, timeout=5)

    # All three drained and the semaphore is back to full capacity.
    assert sem._value == 2
    pending = await fake_redis.xpending(worker.STREAM, worker.GROUP)
    assert pending["pending"] == 0
