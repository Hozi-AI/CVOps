"""
ConsumerLoop — Redis Streams consumer with orphan recovery and graceful shutdown.

Usage:
    loop = ConsumerLoop(
        stream="preprocessing",
        step_types=["step.extract_frames", "step.commit_dataset"],
    )
    await loop.run_forever()
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import Callable, Coroutine
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import select, text

from cvops_api.config import settings
from cvops_api.core.redis_client import close_redis, init_redis
from cvops_api.db.models.runs import Run

from cvops_worker_common.config import worker_settings
from cvops_worker_common.session import async_session_factory

logger = logging.getLogger(__name__)

JobHandler = Callable[[str, str], Coroutine[Any, Any, None]]
# A sync handler receives the full message fields (not a runs row) — used for
# out-of-band doorbells like CVAT completion that carry no job_id/step_type.
SyncHandler = Callable[[dict[str, str]], Coroutine[Any, Any, None]]


class ConsumerLoop:
    def __init__(
        self,
        stream: str,
        step_types: list[str],
        handler: JobHandler | None = None,
        sync_handler: SyncHandler | None = None,
    ) -> None:
        self.stream = stream
        self.step_types = step_types
        self.group = f"worker-{stream}"
        self.consumer_name = f"{socket.gethostname()}-{os.getpid()}"
        self._stop = asyncio.Event()

        from cvops_worker_common.runner import run_job
        self.handler: JobHandler = handler or run_job
        # Messages carrying a "kind" field route here instead of the run handler.
        self.sync_handler: SyncHandler | None = sync_handler

    async def run_forever(self) -> None:
        # The engine's process_step → advance_workflow enqueues the next DAG
        # steps through cvops_api's process-global client (get_redis()), so it
        # must be initialised here too, not just this loop's own connection.
        await init_redis()
        redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        try:
            await self._ensure_group(redis)
            await self._recover_orphans(redis)

            recovery_task = asyncio.create_task(
                self._orphan_recovery_loop(redis)
            )
            try:
                await self._consume_loop(redis)
            finally:
                recovery_task.cancel()
                await asyncio.gather(recovery_task, return_exceptions=True)
        finally:
            await redis.aclose()
            await close_redis()

    def stop(self) -> None:
        self._stop.set()

    async def _ensure_group(self, redis: Redis) -> None:
        try:
            await redis.xgroup_create(
                self.stream, self.group, id="$", mkstream=True
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def _consume_loop(self, redis: Redis) -> None:
        logger.info(
            "Worker %s listening on stream=%r group=%r",
            self.consumer_name, self.stream, self.group,
        )
        while not self._stop.is_set():
            try:
                results = await redis.xreadgroup(
                    groupname=self.group,
                    consumername=self.consumer_name,
                    streams={self.stream: ">"},
                    count=1,
                    block=5000,
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
                    kind = fields.get("kind", "")
                    job_id = fields.get("job_id", "")
                    step_type = fields.get("step_type", "")
                    try:
                        if kind:
                            if self.sync_handler is None:
                                logger.warning(
                                    "No sync_handler for %r message — dropping", kind
                                )
                            else:
                                await self.sync_handler(fields)
                        else:
                            await self.handler(job_id, step_type)
                    except Exception as exc:
                        logger.exception(
                            "Unhandled error in message %s (kind=%r job=%s type=%s): %s",
                            msg_id, kind, job_id, step_type, exc,
                        )
                    finally:
                        await redis.xack(self.stream, self.group, msg_id)

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
