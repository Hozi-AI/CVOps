"""
ConsumerLoop — the Redis Streams consumer every worker runs.

  • Reads its stream through a consumer group (replicas never double-process),
    handling up to ``WORKER_CONCURRENCY`` messages at a time.
  • Retries a message whose handler raised, with back-off, and parks it on
    ``<stream>:dlq`` once ``WORKER_MAX_RETRIES`` is used up (see ``retry.py``).
    Malformed messages go to the DLQ straight away.
  • Re-enqueues pending runs that Redis may have lost (orphan recovery).
  • Stops on SIGTERM/SIGINT after finishing the messages in flight.

Postgres is the authority for job state; Redis is just the doorbell.

Usage:
    loop = ConsumerLoop(stream="preprocessing")  # step types derived from the registry
    await loop.run_forever()
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import uuid
from collections.abc import Callable, Coroutine, Sequence
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import text

from cvops_api.config import settings
from cvops_api.core.redis_client import close_redis, init_redis
from cvops_api.core.registry import registry
from cvops_api.engine.coordinator import queue_for

from cvops_worker_common.config import worker_settings
from cvops_worker_common.retry import RetryQueue, message_identity
from cvops_worker_common.session import async_session_factory

logger = logging.getLogger(__name__)

JobHandler = Callable[[str, str], Coroutine[Any, Any, None]]
# A sync handler receives the full message fields (not a runs row) — used for
# out-of-band doorbells like CVAT completion that carry no job_id/step_type.
SyncHandler = Callable[[dict[str, str]], Coroutine[Any, Any, None]]

BLOCK_MS = 5000
# How often each worker moves due retries back onto its stream.
RETRY_POLL_SECONDS = 1.0


class ConsumerLoop:
    def __init__(
        self,
        stream: str,
        step_types: list[str] | None = None,
        handler: JobHandler | None = None,
        sync_handler: SyncHandler | None = None,
        *,
        concurrency: int | None = None,
        max_retries: int | None = None,
        retry_delays: Sequence[float] | None = None,
    ) -> None:
        self.stream = stream
        # None → every registered step whose queue is this stream, resolved in
        # run_forever (after the worker has registered its steps).
        self.step_types = step_types
        self.group = f"worker-{stream}"
        self.consumer_name = f"{socket.gethostname()}-{os.getpid()}"
        self.concurrency = concurrency or worker_settings.WORKER_CONCURRENCY
        self.max_retries = (
            worker_settings.WORKER_MAX_RETRIES if max_retries is None else max_retries
        )
        self.retry_delays = tuple(
            worker_settings.WORKER_RETRY_DELAYS if retry_delays is None else retry_delays
        )
        self.block_ms = BLOCK_MS
        self._stop = asyncio.Event()

        from cvops_worker_common.runner import run_job

        self.handler: JobHandler = handler or run_job
        # Messages carrying a "kind" field route here instead of the run handler.
        self.sync_handler: SyncHandler | None = sync_handler

    def my_step_types(self) -> list[str]:
        """Registered step types whose queue routes to this stream."""
        return sorted(reg.type_key for reg in registry.all() if queue_for(reg.impl) == self.stream)

    def retry_queue(self, redis: Redis) -> RetryQueue:
        return RetryQueue(
            redis,
            self.stream,
            self.group,
            max_retries=self.max_retries,
            delays=self.retry_delays,
        )

    async def run_forever(self) -> None:
        # The engine's process_step → advance_workflow enqueues the next DAG
        # steps through cvops_api's process-global client (get_redis()), so it
        # must be initialised here too, not just this loop's own connection.
        await init_redis()
        redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        self._install_signal_handlers()
        try:
            if self.step_types is None:
                self.step_types = self.my_step_types()
            logger.info(
                "Worker %s: stream=%r steps=%s concurrency=%d max_retries=%d",
                self.consumer_name,
                self.stream,
                self.step_types,
                self.concurrency,
                self.max_retries,
            )
            await self._ensure_group(redis)
            await self._recover_orphans(redis)

            retries = self.retry_queue(redis)
            background = [
                asyncio.create_task(self._orphan_recovery_loop(redis)),
                asyncio.create_task(self._retry_pump(retries)),
            ]
            try:
                await self._consume_loop(redis, retries)
            finally:
                for task in background:
                    task.cancel()
                await asyncio.gather(*background, return_exceptions=True)
        finally:
            await redis.aclose()
            await close_redis()

    def stop(self) -> None:
        self._stop.set()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.stop)
            except (NotImplementedError, RuntimeError):
                # Windows event loops and non-main threads have no signal handlers.
                pass

    async def _ensure_group(self, redis: Redis) -> None:
        try:
            await redis.xgroup_create(self.stream, self.group, id="$", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def _consume_loop(self, redis: Redis, retries: RetryQueue) -> None:
        logger.info(
            "Worker %s listening on stream=%r group=%r",
            self.consumer_name,
            self.stream,
            self.group,
        )
        sem = asyncio.Semaphore(self.concurrency)
        inflight: set[asyncio.Task[None]] = set()
        while not self._stop.is_set():
            try:
                results = await redis.xreadgroup(
                    groupname=self.group,
                    consumername=self.consumer_name,
                    streams={self.stream: ">"},
                    count=self.concurrency,
                    block=self.block_ms,
                )
            except asyncio.CancelledError:
                break
            except RedisTimeoutError:
                # The blocking read elapsed with no message — normal idle tick,
                # not an error. Loop straight back so delivery stays responsive
                # (no backoff penalty, no log spam).
                continue
            except RedisConnectionError as exc:
                logger.warning("Redis connection lost: %s — retrying in 2s", exc)
                await asyncio.sleep(2)
                continue
            except Exception as exc:
                logger.warning("XREADGROUP error: %s — retrying in 2s", exc)
                await asyncio.sleep(2)
                continue

            if not results:
                continue

            for _stream, messages in results:
                for msg_id, fields in messages:
                    await sem.acquire()
                    task = asyncio.create_task(self._handle(retries, msg_id, fields, sem))
                    inflight.add(task)
                    task.add_done_callback(inflight.discard)

        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)

    async def _handle(
        self,
        retries: RetryQueue,
        msg_id: str,
        fields: dict[str, str],
        sem: asyncio.Semaphore,
    ) -> None:
        try:
            await self._dispatch(retries, msg_id, fields)
        except Exception:
            # Only reachable when Redis failed while acking or scheduling the
            # retry. The message stays un-acked; a run it pointed at stays
            # pending in Postgres, so orphan recovery re-enqueues it.
            logger.exception("Could not settle message %s on %r", msg_id, self.stream)
        finally:
            sem.release()

    async def _dispatch(self, retries: RetryQueue, msg_id: str, fields: dict[str, str]) -> None:
        reason = self._reject_reason(fields)
        if reason is not None:
            # Malformed: retrying can't fix it, so park it for a human.
            await retries.dead_letter(msg_id, fields, reason)
            return
        try:
            kind = fields.get("kind")
            if kind and self.sync_handler is not None:
                await self.sync_handler(fields)
            else:
                await self.handler(fields["job_id"], fields.get("step_type", ""))
        except Exception as exc:
            logger.exception("Handler failed for message %s (%s)", msg_id, message_identity(fields))
            await retries.failed(msg_id, fields, f"{type(exc).__name__}: {exc}")
        else:
            await retries.succeeded(msg_id, fields)

    def _reject_reason(self, fields: dict[str, str]) -> str | None:
        """Why a message is malformed, or None if it can be handled."""
        kind = fields.get("kind")
        if kind:
            return None if self.sync_handler is not None else f"no handler for kind {kind!r}"
        job_id = fields.get("job_id")
        if not job_id:
            return "missing job_id"
        try:
            uuid.UUID(job_id)
        except ValueError:
            return f"invalid job_id {job_id!r}"
        return None

    async def _retry_pump(self, retries: RetryQueue) -> None:
        while not self._stop.is_set():
            try:
                await retries.requeue_due()
            except Exception as exc:
                logger.warning("Retry requeue error: %s", exc)
            await asyncio.sleep(RETRY_POLL_SECONDS)

    async def _orphan_recovery_loop(self, redis: Redis) -> None:
        interval = worker_settings.ORPHAN_RECOVERY_INTERVAL
        while not self._stop.is_set():
            await asyncio.sleep(interval)
            try:
                await self._recover_orphans(redis)
            except Exception as exc:
                logger.warning("Orphan recovery error: %s", exc)

    async def _recover_orphans(self, redis: Redis) -> None:
        """Re-enqueue pending jobs that are old enough to have been lost by Redis."""
        age = worker_settings.ORPHAN_PENDING_AGE_SECONDS
        async with async_session_factory() as session:
            result = await session.execute(
                text(
                    """
                    SELECT id, step_type FROM runs
                    WHERE status = 'pending'
                      AND step_type = ANY(:step_types)
                      AND created_at < now() - make_interval(secs => :age)
                    """
                ),
                {"step_types": self.step_types, "age": age},
            )
            rows = result.all()

        for row in rows:
            job_id, step_type = str(row.id), row.step_type
            await redis.xadd(
                self.stream,
                {"job_id": job_id, "step_type": step_type, "queue": self.stream},
            )
            logger.info("Re-enqueued orphan job %s (%s)", job_id, step_type)
