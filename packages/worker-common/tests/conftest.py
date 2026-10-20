"""Fixtures for worker-common.

No Postgres here: the consumer and retry logic only need the Redis doorbell,
so ``fakeredis`` stands in as a real in-memory Streams backend and handlers are
plain async functions. The claim path against a real database is covered by
the API suite (``services/api/tests/worker/test_worker_loop.py``).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

import fakeredis.aioredis
import pytest

from cvops_worker_common import ConsumerLoop
from cvops_worker_common.retry import RetryQueue

STREAM = "teststream"


@pytest.fixture
async def fake_redis() -> AsyncIterator[fakeredis.aioredis.FakeRedis]:
    """fakeredis with ``decode_responses=True``, like the production client.

    fakeredis returns at once from a *blocking* xreadgroup on an empty stream,
    so the consume loop would busy-spin and starve the event loop. Real Redis
    parks for ``block`` ms; we model that by yielding briefly on an empty read.
    """
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    real_xreadgroup = r.xreadgroup

    async def _yielding_xreadgroup(*args, **kwargs):  # type: ignore[no-untyped-def]
        resp = await real_xreadgroup(*args, **kwargs)
        if not resp and kwargs.get("block"):
            await asyncio.sleep(0.01)
        return resp

    r.xreadgroup = _yielding_xreadgroup  # type: ignore[method-assign]
    yield r
    await r.aclose()


def make_loop(**kwargs) -> ConsumerLoop:  # type: ignore[no-untyped-def]
    """A loop on the test stream with instant retries and a short blocking read."""
    kwargs.setdefault("step_types", [])
    kwargs.setdefault("retry_delays", (0.0,))
    kwargs.setdefault("max_retries", 3)
    loop = ConsumerLoop(stream=STREAM, **kwargs)
    loop.block_ms = 50
    return loop


async def drive(
    loop: ConsumerLoop,
    redis: fakeredis.aioredis.FakeRedis,
    until: Callable[[], Awaitable[bool]],
    *,
    pump: bool = False,
    timeout: float = 5.0,
) -> RetryQueue:
    """Run the consume loop (and optionally the retry pump) until ``until()``."""
    await loop._ensure_group(redis)
    retries = loop.retry_queue(redis)
    tasks = [asyncio.create_task(loop._consume_loop(redis, retries))]
    if pump:
        tasks.append(asyncio.create_task(_fast_pump(loop, retries)))
    try:
        async with asyncio.timeout(timeout):
            while not await until():
                await asyncio.sleep(0.02)
    finally:
        loop.stop()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    return retries


async def _fast_pump(loop: ConsumerLoop, retries: RetryQueue) -> None:
    while not loop._stop.is_set():
        await retries.requeue_due()
        await asyncio.sleep(0.02)


async def pending(redis: fakeredis.aioredis.FakeRedis) -> int:
    """Un-acked entries in the test group's pending-entries list."""
    return int((await redis.xpending(STREAM, f"worker-{STREAM}"))["pending"])
