"""RetryQueue: attempt counting, back-off scheduling, DLQ, and the requeue pump."""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import pytest
from redis.exceptions import ResponseError

from cvops_worker_common.retry import RETRY_COUNTER_TTL_SECONDS, RetryQueue, message_identity

from tests.conftest import STREAM

GROUP = f"worker-{STREAM}"


async def _read_one(redis) -> tuple[str, dict[str, str]]:  # type: ignore[no-untyped-def]
    """Deliver one message to the group, as XREADGROUP would in the loop."""
    resp = await redis.xreadgroup(GROUP, "c1", streams={STREAM: ">"}, count=1)
    return resp[0][1][0]  # type: ignore[no-any-return]


@pytest.fixture
async def queue(fake_redis) -> RetryQueue:  # type: ignore[no-untyped-def]
    await fake_redis.xgroup_create(STREAM, GROUP, id="$", mkstream=True)
    return RetryQueue(fake_redis, STREAM, GROUP, max_retries=3, delays=(1.0, 5.0, 30.0))


# ── message identity ─────────────────────────────────────────────────────────


def test_identity_of_a_run_doorbell_is_its_job_id() -> None:
    job_id = str(uuid.uuid4())
    assert message_identity({"job_id": job_id, "step_type": "step.x"}) == f"job:{job_id}"


def test_identity_of_a_kind_doorbell_depends_on_its_fields() -> None:
    a = message_identity({"kind": "cvat_sync", "cvat_task_id": "1"})
    assert a.startswith("cvat_sync:")
    assert a == message_identity({"cvat_task_id": "1", "kind": "cvat_sync"})  # order-free
    assert a != message_identity({"kind": "cvat_sync", "cvat_task_id": "2"})


def test_identity_ignores_dlq_metadata_so_a_replay_keeps_it() -> None:
    fields = {"kind": "cvat_sync", "cvat_task_id": "1"}
    replayed = {**fields, "original_msg_id": "1-0", "dlq_reason": "boom", "attempts": "4"}
    assert message_identity(replayed) == message_identity(fields)


# ── back-off ─────────────────────────────────────────────────────────────────


def test_delay_follows_the_schedule_and_repeats_the_last(fake_redis) -> None:  # type: ignore[no-untyped-def]
    q = RetryQueue(fake_redis, STREAM, GROUP, max_retries=5, delays=(1.0, 5.0, 30.0))
    assert [q.delay_for(n) for n in (1, 2, 3, 4, 5)] == [1.0, 5.0, 30.0, 30.0, 30.0]


def test_empty_delays_are_rejected(fake_redis) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        RetryQueue(fake_redis, STREAM, GROUP, max_retries=3, delays=())


# ── failed / succeeded / dead_letter ─────────────────────────────────────────


async def test_failure_schedules_a_retry_in_redis_and_acks(queue, fake_redis) -> None:  # type: ignore[no-untyped-def]
    fields = {"job_id": str(uuid.uuid4()), "step_type": "step.x"}
    await fake_redis.xadd(STREAM, fields)
    msg_id, got = await _read_one(fake_redis)

    before = time.time()
    assert await queue.failed(msg_id, got, "boom") is True

    # Acked, and parked in the retry set (not back on the stream yet), due ~1s out.
    assert (await fake_redis.xpending(STREAM, GROUP))["pending"] == 0
    assert await fake_redis.xlen(STREAM) == 1
    [(member, due)] = await fake_redis.zrange(queue.retry_key, 0, -1, withscores=True)
    assert json.loads(member)["fields"] == fields
    assert before + 1.0 <= due <= time.time() + 1.0
    # Counter set, with a TTL so it can't leak.
    counter = f"worker:retry:{STREAM}:job:{fields['job_id']}"
    assert await fake_redis.get(counter) == "1"
    assert 0 < await fake_redis.ttl(counter) <= RETRY_COUNTER_TTL_SECONDS


async def test_retries_exhausted_dead_letters_with_the_reason(queue, fake_redis) -> None:  # type: ignore[no-untyped-def]
    fields = {"job_id": str(uuid.uuid4()), "step_type": "step.x"}
    counter = f"worker:retry:{STREAM}:job:{fields['job_id']}"
    await fake_redis.set(counter, 3)  # three retries already spent
    await fake_redis.xadd(STREAM, fields)
    msg_id, got = await _read_one(fake_redis)

    assert await queue.failed(msg_id, got, "still broken") is False

    [(_dlq_id, entry)] = await fake_redis.xrange(queue.dlq_stream)
    assert entry == {
        **fields,
        "original_msg_id": msg_id,
        "dlq_reason": "still broken",
        "attempts": "4",
    }
    assert (await fake_redis.xpending(STREAM, GROUP))["pending"] == 0
    assert await fake_redis.get(counter) is None
    assert await fake_redis.zcard(queue.retry_key) == 0


async def test_success_acks_and_clears_the_counter(queue, fake_redis) -> None:  # type: ignore[no-untyped-def]
    fields = {"job_id": str(uuid.uuid4())}
    counter = f"worker:retry:{STREAM}:job:{fields['job_id']}"
    await fake_redis.set(counter, 2)
    await fake_redis.xadd(STREAM, fields)
    msg_id, got = await _read_one(fake_redis)

    await queue.succeeded(msg_id, got)

    assert (await fake_redis.xpending(STREAM, GROUP))["pending"] == 0
    assert await fake_redis.get(counter) is None


async def test_dead_letter_truncates_a_huge_reason(queue, fake_redis) -> None:  # type: ignore[no-untyped-def]
    await fake_redis.xadd(STREAM, {"step_type": "step.x"})
    msg_id, got = await _read_one(fake_redis)

    await queue.dead_letter(msg_id, got, "x" * 5000)

    [(_id, entry)] = await fake_redis.xrange(queue.dlq_stream)
    assert len(entry["dlq_reason"]) == 1000
    assert entry["attempts"] == "0"


# ── requeue pump ─────────────────────────────────────────────────────────────


async def test_requeue_moves_only_due_retries(queue, fake_redis) -> None:  # type: ignore[no-untyped-def]
    now = time.time()
    due = json.dumps({"msg_id": "1-0", "fields": {"job_id": "a"}}, sort_keys=True)
    later = json.dumps({"msg_id": "2-0", "fields": {"job_id": "b"}}, sort_keys=True)
    await fake_redis.zadd(queue.retry_key, {due: now - 1, later: now + 60})

    assert await queue.requeue_due(now) == 1

    entries = await fake_redis.xrange(STREAM)
    assert [fields for _id, fields in entries] == [{"job_id": "a"}]
    assert await fake_redis.zrange(queue.retry_key, 0, -1) == [later]


async def test_concurrent_pumps_requeue_each_retry_once(queue, fake_redis, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Every worker runs a pump; the script's membership check keeps two of them from both re-sending."""
    members = {
        json.dumps({"msg_id": f"{i}-0", "fields": {"job_id": str(i)}}, sort_keys=True): 0
        for i in range(20)
    }
    await fake_redis.zadd(queue.retry_key, members)

    # Make both pumps read the same due entries before either removes any —
    # the worst-case interleaving of two workers polling at the same moment.
    barrier = asyncio.Barrier(2)
    real_zrangebyscore = fake_redis.zrangebyscore

    async def _racing_zrangebyscore(*args, **kwargs):  # type: ignore[no-untyped-def]
        due = await real_zrangebyscore(*args, **kwargs)
        await barrier.wait()
        return due

    monkeypatch.setattr(fake_redis, "zrangebyscore", _racing_zrangebyscore)

    moved = await asyncio.gather(queue.requeue_due(), queue.requeue_due())

    assert sum(moved) == 20
    assert await fake_redis.xlen(STREAM) == 20


async def test_a_failed_resend_keeps_the_retry(fake_redis) -> None:  # type: ignore[no-untyped-def]
    """Re-send and remove are one script, re-send first: if the XADD fails, the
    entry stays in the retry set. Matters most for cvat_sync doorbells, which no
    Postgres row can recover."""
    q = RetryQueue(fake_redis, "broken", "g", max_retries=3, delays=(1.0,))
    fields = {"kind": "cvat_sync", "cvat_task_id": "7"}
    member = json.dumps({"msg_id": "1-0", "fields": fields}, sort_keys=True)
    await fake_redis.zadd(q.retry_key, {member: 0})
    await fake_redis.set("broken", "not a stream")  # XADD → WRONGTYPE

    with pytest.raises(ResponseError):
        await q.requeue_due()
    assert await fake_redis.zrange(q.retry_key, 0, -1) == [member]

    await fake_redis.delete("broken")
    assert await q.requeue_due() == 1
    assert [f for _id, f in await fake_redis.xrange("broken")] == [fields]
    assert await fake_redis.zcard(q.retry_key) == 0
