"""ConsumerLoop: dispatch, retries, DLQ, concurrency and shutdown, end to end
against an in-memory Redis Stream."""

from __future__ import annotations

import asyncio
import uuid

from cvops_worker_common import ConsumerLoop

from tests.conftest import STREAM, drive, make_loop, pending

DLQ = f"{STREAM}:dlq"


def _job() -> dict[str, str]:
    return {"job_id": str(uuid.uuid4()), "step_type": "step.x", "queue": STREAM}


# ── dispatch ─────────────────────────────────────────────────────────────────


async def test_run_doorbell_reaches_the_handler_and_is_acked(fake_redis) -> None:  # type: ignore[no-untyped-def]
    seen: list[tuple[str, str]] = []

    async def handler(job_id: str, step_type: str) -> None:
        seen.append((job_id, step_type))

    loop = make_loop(handler=handler)
    job = _job()
    await loop._ensure_group(fake_redis)
    await fake_redis.xadd(STREAM, job)

    async def done() -> bool:
        return bool(seen) and await pending(fake_redis) == 0

    await drive(loop, fake_redis, done)
    assert seen == [(job["job_id"], "step.x")]
    assert await fake_redis.xlen(DLQ) == 0


async def test_kind_doorbell_goes_to_the_sync_handler_with_all_fields(fake_redis) -> None:  # type: ignore[no-untyped-def]
    seen: list[dict[str, str]] = []

    async def handler(job_id: str, step_type: str) -> None:
        raise AssertionError("the run handler must not see a kind doorbell")

    async def sync_handler(fields: dict[str, str]) -> None:
        seen.append(fields)

    loop = make_loop(handler=handler, sync_handler=sync_handler)
    await loop._ensure_group(fake_redis)
    await fake_redis.xadd(STREAM, {"kind": "cvat_sync", "cvat_task_id": "42"})

    async def done() -> bool:
        return bool(seen) and await pending(fake_redis) == 0

    await drive(loop, fake_redis, done)
    assert seen == [{"kind": "cvat_sync", "cvat_task_id": "42"}]


# ── malformed messages → DLQ straight away ───────────────────────────────────


async def _dead_letter_reason(fake_redis, fields: dict[str, str], **loop_kwargs) -> str:  # type: ignore[no-untyped-def]
    called = False

    async def handler(job_id: str, step_type: str) -> None:
        nonlocal called
        called = True

    loop = make_loop(handler=handler, **loop_kwargs)
    await loop._ensure_group(fake_redis)
    await fake_redis.xadd(STREAM, fields)

    async def done() -> bool:
        return await fake_redis.xlen(DLQ) > 0

    await drive(loop, fake_redis, done)
    assert not called
    assert await pending(fake_redis) == 0
    [(_id, entry)] = await fake_redis.xrange(DLQ)
    assert entry["attempts"] == "0"
    return str(entry["dlq_reason"])


async def test_missing_job_id_is_dead_lettered(fake_redis) -> None:  # type: ignore[no-untyped-def]
    assert await _dead_letter_reason(fake_redis, {"step_type": "step.x"}) == "missing job_id"


async def test_invalid_job_id_is_dead_lettered(fake_redis) -> None:  # type: ignore[no-untyped-def]
    reason = await _dead_letter_reason(fake_redis, {"job_id": "not-a-uuid"})
    assert reason == "invalid job_id 'not-a-uuid'"


async def test_kind_without_a_sync_handler_is_dead_lettered(fake_redis) -> None:  # type: ignore[no-untyped-def]
    reason = await _dead_letter_reason(fake_redis, {"kind": "cvat_sync", "cvat_task_id": "1"})
    assert reason == "no handler for kind 'cvat_sync'"


# ── retries ──────────────────────────────────────────────────────────────────


async def test_transient_failure_is_retried_until_it_succeeds(fake_redis) -> None:  # type: ignore[no-untyped-def]
    calls = 0

    async def handler(job_id: str, step_type: str) -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionError("postgres restarting")

    loop = make_loop(handler=handler)
    job = _job()
    await loop._ensure_group(fake_redis)
    await fake_redis.xadd(STREAM, job)

    async def done() -> bool:
        return calls == 3 and await pending(fake_redis) == 0

    retries = await drive(loop, fake_redis, done, pump=True)
    assert calls == 3
    assert await fake_redis.xlen(DLQ) == 0
    assert await fake_redis.zcard(retries.retry_key) == 0
    assert await fake_redis.get(f"worker:retry:{STREAM}:job:{job['job_id']}") is None


async def test_persistent_failure_lands_in_the_dlq_after_max_retries(fake_redis) -> None:  # type: ignore[no-untyped-def]
    calls = 0

    async def handler(job_id: str, step_type: str) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("always broken")

    loop = make_loop(handler=handler, max_retries=2)
    job = _job()
    await loop._ensure_group(fake_redis)
    await fake_redis.xadd(STREAM, job)

    async def done() -> bool:
        return await fake_redis.xlen(DLQ) > 0

    await drive(loop, fake_redis, done, pump=True)
    assert calls == 3  # the first try + 2 retries
    [(_id, entry)] = await fake_redis.xrange(DLQ)
    assert entry["job_id"] == job["job_id"]
    assert entry["dlq_reason"] == "RuntimeError: always broken"
    assert entry["attempts"] == "3"
    assert await pending(fake_redis) == 0


async def test_failing_sync_doorbell_is_retried_not_dead_lettered(fake_redis) -> None:  # type: ignore[no-untyped-def]
    """A cvat_sync doorbell carries no job_id. It must be retried like any other
    message (counted under its own identity), not treated as malformed: it has
    no Postgres row, so orphan recovery can never re-send it if it's lost."""
    calls = 0

    async def sync_handler(fields: dict[str, str]) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("CVAT unreachable")

    loop = make_loop(sync_handler=sync_handler)
    await loop._ensure_group(fake_redis)
    await fake_redis.xadd(STREAM, {"kind": "cvat_sync", "cvat_task_id": "7"})

    async def done() -> bool:
        return calls == 2 and await pending(fake_redis) == 0

    await drive(loop, fake_redis, done, pump=True)
    assert calls == 2
    assert await fake_redis.xlen(DLQ) == 0


async def test_retry_waits_for_its_back_off(fake_redis) -> None:  # type: ignore[no-untyped-def]
    """With a real delay the message sits in the retry set, not on the stream."""

    async def handler(job_id: str, step_type: str) -> None:
        raise RuntimeError("boom")

    loop = make_loop(handler=handler, retry_delays=(60.0,))
    await loop._ensure_group(fake_redis)
    await fake_redis.xadd(STREAM, _job())
    retries = loop.retry_queue(fake_redis)

    async def done() -> bool:
        return await fake_redis.zcard(retries.retry_key) == 1

    await drive(loop, fake_redis, done, pump=True)
    assert await retries.requeue_due() == 0  # not due for another minute
    assert await fake_redis.xlen(STREAM) == 1  # only the original message


# ── concurrency and shutdown ─────────────────────────────────────────────────


async def test_messages_run_concurrently_up_to_the_limit(fake_redis) -> None:  # type: ignore[no-untyped-def]
    running = 0
    peak = 0
    finished = 0
    release = asyncio.Event()

    async def handler(job_id: str, step_type: str) -> None:
        nonlocal running, peak, finished
        running += 1
        peak = max(peak, running)
        await release.wait()
        running -= 1
        finished += 1

    loop = make_loop(handler=handler, concurrency=2)
    await loop._ensure_group(fake_redis)
    for _ in range(4):
        await fake_redis.xadd(STREAM, _job())

    async def all_done() -> bool:
        # Hold the handlers until two run at once, then let everything finish.
        if running == 2:
            release.set()
        return finished == 4 and await pending(fake_redis) == 0

    await drive(loop, fake_redis, all_done)
    assert peak == 2


async def test_stop_drains_in_flight_messages(fake_redis) -> None:  # type: ignore[no-untyped-def]
    started = asyncio.Event()
    finished = False

    async def handler(job_id: str, step_type: str) -> None:
        nonlocal finished
        started.set()
        await asyncio.sleep(0.2)
        finished = True

    loop = make_loop(handler=handler)
    await loop._ensure_group(fake_redis)
    await fake_redis.xadd(STREAM, _job())

    async def is_started() -> bool:
        return started.is_set()

    # drive() calls stop() as soon as the handler starts; the loop must still
    # wait for it and ack it.
    await drive(loop, fake_redis, is_started)
    assert finished
    assert await pending(fake_redis) == 0


# ── step types ───────────────────────────────────────────────────────────────


class _Impl:
    def __init__(self, queue: str) -> None:
        self.queue = queue


class _Reg:
    def __init__(self, type_key: str, queue: str) -> None:
        self.type_key = type_key
        self.impl = _Impl(queue)


def test_step_types_default_to_the_steps_routed_to_this_stream(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import cvops_worker_common.consumer as consumer

    regs = [
        _Reg("step.extract_frames", ""),  # empty queue → the default "preprocessing"
        _Reg("step.commit_dataset", "preprocessing"),
        _Reg("step.train", "training"),
    ]
    monkeypatch.setattr(consumer, "registry", type("R", (), {"all": staticmethod(lambda: regs)}))

    assert ConsumerLoop(stream="preprocessing").my_step_types() == [
        "step.commit_dataset",
        "step.extract_frames",
    ]
    assert ConsumerLoop(stream="training").my_step_types() == ["step.train"]
    assert ConsumerLoop(stream="cvat").my_step_types() == []


# ── background tasks ─────────────────────────────────────────────────────────


async def test_background_tasks_run_alongside_and_stop_with_the_loop(
    fake_redis, monkeypatch, caplog
) -> None:  # type: ignore[no-untyped-def]
    """worker-cvat serves HTTP this way. A side task gets the stop event and is
    awaited on shutdown; one that crashes is logged at once and consuming goes on."""
    import cvops_worker_common.consumer as consumer

    async def _noop(*_a, **_k) -> None:  # type: ignore[no-untyped-def]
        return None

    # run_forever without the API's global Redis or Postgres.
    monkeypatch.setattr(consumer, "init_redis", _noop)
    monkeypatch.setattr(consumer, "close_redis", _noop)
    monkeypatch.setattr(consumer.Redis, "from_url", staticmethod(lambda *_a, **_k: fake_redis))
    monkeypatch.setattr(fake_redis, "aclose", _noop)  # the fixture closes it

    started = asyncio.Event()
    shut_down: list[str] = []

    async def server(stop: asyncio.Event) -> None:
        started.set()
        await stop.wait()
        shut_down.append("server")

    async def broken(stop: asyncio.Event) -> None:
        raise OSError("port already in use")

    handled: list[str] = []

    async def handler(job_id: str, step_type: str) -> None:
        handled.append(job_id)

    loop = make_loop(handler=handler, background=[broken, server])
    monkeypatch.setattr(loop, "_recover_orphans", _noop)
    task = asyncio.create_task(loop.run_forever())

    await asyncio.wait_for(started.wait(), 5)
    job = _job()
    await fake_redis.xadd(STREAM, job)
    async with asyncio.timeout(5):
        while not handled:
            await asyncio.sleep(0.02)
    assert handled == [job["job_id"]]  # still consuming after `broken` crashed
    assert "Background task broken failed" in caplog.text

    loop.stop()
    await asyncio.wait_for(task, 5)
    assert shut_down == ["server"]
