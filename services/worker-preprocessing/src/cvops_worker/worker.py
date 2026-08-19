"""Redis-Streams preprocessing worker.

One process consumes the ``preprocessing`` stream via a named consumer group
(so replicas don't double-process), runs a single step per message through the
shared ``process_step`` coordinator, and acks. Postgres is the authority for job
state; Redis is just the doorbell.

Connects to PostgreSQL and Garage directly through ``cvops_api`` settings — it
never calls the API.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import uuid

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import select

from cvops_api.core.redis_client import init_redis, close_redis, get_redis
from cvops_api.core.registry import registry
from cvops_api.db.models.runs import Run
from cvops_api.db.session import async_session_factory, engine
from cvops_api.engine.coordinator import (
    enqueue_step,
    find_orphan_step_runs,
    process_step,
    queue_for,
)

# Worker-specific config (not part of the API's settings surface).
STREAM = os.getenv("REDIS_STREAM", "preprocessing")
CONCURRENCY = int(os.getenv("WORKER_CONCURRENCY", "4"))
MAX_RETRIES = int(os.getenv("WORKER_MAX_RETRIES", "3"))
ORPHAN_INTERVAL_SECONDS = 60
ORPHAN_MIN_AGE_SECONDS = 30
BLOCK_MS = 5000

DLQ_STREAM = f"{STREAM}:dlq"
_RETRY_DELAYS: tuple[float, ...] = (1.0, 5.0, 30.0)
_RETRY_KEY_PREFIX = "worker:retry:"
_RETRY_KEY_TTL = 3600  # expire stale counters after 1 hour

GROUP = f"worker-{STREAM}"
CONSUMER = f"worker-{socket.gethostname()}-{os.getpid()}"
# Steps with no explicit actor (worker-driven) are attributed to this system id.
SYSTEM_ACTOR_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")


async def _send_to_dlq(
    redis: object, msg_id: str, fields: dict[str, str], reason: str
) -> None:
    await redis.xadd(DLQ_STREAM, {**fields, "original_msg_id": msg_id, "dlq_reason": reason})  # type: ignore[union-attr]
    print(f"[worker] DLQ ← msg_id={msg_id} reason={reason!r}", flush=True)


def _my_step_types() -> set[str]:
    """Step types whose queue routes to this worker's stream."""
    return {reg.type_key for reg in registry.all() if queue_for(reg.impl) == STREAM}


async def _ensure_group() -> None:
    redis = get_redis()
    try:
        await redis.xgroup_create(STREAM, GROUP, id="$", mkstream=True)
    except Exception as exc:  # noqa: BLE001 — BUSYGROUP just means it already exists
        if "BUSYGROUP" not in str(exc):
            raise


async def _claim_and_run(job_id: str) -> None:
    """Claim one pending run and execute it in its own session."""
    run_id = uuid.UUID(job_id)
    async with async_session_factory() as session:
        # FOR UPDATE SKIP LOCKED + the status guard: at most one worker runs a
        # given pending run. A re-enqueued or already-finished job is a no-op.
        r = await session.execute(
            select(Run)
            .where(Run.id == run_id, Run.status == "pending")
            .with_for_update(skip_locked=True)
        )
        claimed = r.scalar_one_or_none()
        if claimed is None:
            return
        await process_step(session, run_id, SYSTEM_ACTOR_ID)


async def _consume_loop(stop: asyncio.Event, sem: asyncio.Semaphore) -> None:
    redis = get_redis()
    inflight: set[asyncio.Task[None]] = set()

    async def _handle(msg_id: str, fields: dict[str, str]) -> None:
        job_id = fields.get("job_id")
        requeue_delay: float = 0.0
        requeue_fields: dict[str, str] | None = None

        try:
            if job_id is None:
                # Poison-pill: structurally invalid message → DLQ immediately.
                await _send_to_dlq(redis, msg_id, fields, "missing job_id")
            else:
                retry_key = f"{_RETRY_KEY_PREFIX}{job_id}"
                try:
                    await _claim_and_run(job_id)
                    await redis.delete(retry_key)
                except Exception as exc:  # noqa: BLE001
                    attempt = int(await redis.incr(retry_key))
                    await redis.expire(retry_key, _RETRY_KEY_TTL)
                    if attempt <= MAX_RETRIES:
                        requeue_delay = _RETRY_DELAYS[min(attempt - 1, len(_RETRY_DELAYS) - 1)]
                        requeue_fields = dict(fields)
                        print(
                            f"[worker] retry {attempt}/{MAX_RETRIES} job_id={job_id}"
                            f" in {requeue_delay:.0f}s: {exc}",
                            flush=True,
                        )
                    else:
                        await _send_to_dlq(redis, msg_id, fields, str(exc))
                        await redis.delete(retry_key)
        finally:
            await redis.xack(STREAM, GROUP, msg_id)
            sem.release()

        # Re-enqueue after releasing the semaphore so the backoff sleep
        # does not hold a concurrency slot.
        if requeue_fields is not None:
            if requeue_delay:
                await asyncio.sleep(requeue_delay)
            await redis.xadd(STREAM, requeue_fields)

    while not stop.is_set():
        try:
            resp = await redis.xreadgroup(
                GROUP, CONSUMER, streams={STREAM: ">"}, count=CONCURRENCY, block=BLOCK_MS
            )
        except RedisTimeoutError:
            # The blocking read elapsed or was interrupted at shutdown (redis-py
            # surfaces a cancelled blocking read as TimeoutError). Re-check stop
            # and retry rather than crashing the worker.
            continue
        except (RedisConnectionError, asyncio.CancelledError):
            break
        if not resp:
            continue
        for _stream, messages in resp:
            for msg_id, fields in messages:
                await sem.acquire()
                task = asyncio.create_task(_handle(msg_id, fields))
                inflight.add(task)
                task.add_done_callback(inflight.discard)

    if inflight:
        await asyncio.gather(*inflight, return_exceptions=True)


async def _orphan_recovery_loop(stop: asyncio.Event) -> None:
    """Re-enqueue pending runs Redis may have dropped on restart.

    The consumer group + the ``status='pending'`` claim guard make re-enqueue
    idempotent: a job already in Redis or already running is not double-run.
    """
    my_types = _my_step_types()
    if not my_types:
        return
    while not stop.is_set():
        try:
            async with async_session_factory() as session:
                orphans = await find_orphan_step_runs(
                    session, my_types, ORPHAN_MIN_AGE_SECONDS
                )
            for run_id, step_type in orphans:
                await enqueue_step(run_id, step_type, STREAM)
        except Exception as exc:  # noqa: BLE001 — never let recovery kill the worker
            print(f"[orphan-recovery] error: {exc}", flush=True)
        # Sleep in short slices so shutdown is responsive.
        for _ in range(ORPHAN_INTERVAL_SECONDS):
            if stop.is_set():
                break
            await asyncio.sleep(1)


async def run() -> None:
    await init_redis()
    try:
        from cvops_steps import register_all

        register_all()
    except ImportError:
        print("[worker] cvops_steps not importable — registry is empty", flush=True)

    await _ensure_group()
    print(
        f"[worker] {CONSUMER} joined group {GROUP!r} on stream {STREAM!r}; "
        f"steps={sorted(_my_step_types())} concurrency={CONCURRENCY}",
        flush=True,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    sem = asyncio.Semaphore(CONCURRENCY)
    orphan_task = asyncio.create_task(_orphan_recovery_loop(stop))
    try:
        await _consume_loop(stop, sem)
    finally:
        stop.set()
        await orphan_task
        await close_redis()
        await engine.dispose()
        print("[worker] shutdown complete", flush=True)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
