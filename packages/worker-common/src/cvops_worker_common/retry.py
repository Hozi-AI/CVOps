"""Retries and the dead-letter queue (DLQ) for one Redis stream.

When a worker's handler raises, something *around* the step failed (Postgres or
Redis dropped, CVAT unreachable): ``process_step`` already records a step's own
exceptions as a failed run and returns normally. Failures like that are usually
transient, so the message is tried again after a back-off. Only after
``max_retries`` retries is it parked on ``<stream>:dlq`` for a human to inspect.

Retries wait in Redis, not in the worker's memory. A failed message goes into
the sorted set ``<stream>:retry`` (score = when it is due) in the same
transaction that acks it, and every worker's ``requeue_due`` pump moves due
entries back onto the stream. A worker that dies mid back-off loses nothing:
another worker's pump re-sends the message. Moving an entry is one Lua script
(re-send, then remove), so no crash or Redis error can drop it in between.

Attempts are counted per message *identity*, not per Redis message id (each
retry is a new message with a new id): ``job:<job_id>`` for run doorbells, and
the kind plus a hash of the fields for out-of-band doorbells such as
``{kind: "cvat_sync", cvat_task_id}``, which carry no job_id.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Sequence
from typing import Any, cast

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

# Forget a message's attempt count an hour after its last failure.
RETRY_COUNTER_TTL_SECONDS = 3600
# Cap the DLQ (approximately) so a failure storm can't fill Redis.
DLQ_MAXLEN = 10_000
# Fields the DLQ adds; ignored by message_identity so a replayed entry keeps its identity.
_DLQ_FIELDS = frozenset({"original_msg_id", "dlq_reason", "attempts"})
_REQUEUE_BATCH = 100

# Move one retry entry back onto the stream, atomically. KEYS = [retry set,
# stream]; ARGV = [member, field1, value1, ...]. The membership check is the
# claim: when several workers' pumps see the same entry, only the first script
# to run finds it. XADD comes before ZREM because Redis does not roll back a
# script that errors part-way: if the XADD fails, the entry stays for next time.
_REQUEUE_ONE = """
if not redis.call('ZSCORE', KEYS[1], ARGV[1]) then
  return 0
end
redis.call('XADD', KEYS[2], '*', unpack(ARGV, 2))
redis.call('ZREM', KEYS[1], ARGV[1])
return 1
"""


def message_identity(fields: dict[str, str]) -> str:
    """A stable name for "this message", the same across its retries."""
    kind = fields.get("kind", "")
    if not kind:
        return f"job:{fields.get('job_id', '')}"
    payload = {k: v for k, v in fields.items() if k not in _DLQ_FIELDS}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return f"{kind}:{digest[:16]}"


class RetryQueue:
    """Acks every message exactly once: as done, as a scheduled retry, or into the DLQ."""

    def __init__(
        self,
        redis: Redis,
        stream: str,
        group: str,
        *,
        max_retries: int,
        delays: Sequence[float],
    ) -> None:
        if not delays:
            raise ValueError("retry delays must not be empty")
        self.redis = redis
        self.stream = stream
        self.group = group
        self.max_retries = max_retries
        self.delays = tuple(delays)
        self.dlq_stream = f"{stream}:dlq"
        self.retry_key = f"{stream}:retry"
        self._requeue_one = redis.register_script(_REQUEUE_ONE)

    def _counter_key(self, fields: dict[str, str]) -> str:
        return f"worker:retry:{self.stream}:{message_identity(fields)}"

    def delay_for(self, attempt: int) -> float:
        """Back-off before retry ``attempt`` (1-based); the last delay repeats."""
        return self.delays[min(attempt, len(self.delays)) - 1]

    async def succeeded(self, msg_id: str, fields: dict[str, str]) -> None:
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.xack(self.stream, self.group, msg_id)
            pipe.delete(self._counter_key(fields))
            await pipe.execute()

    async def failed(self, msg_id: str, fields: dict[str, str], error: str) -> bool:
        """Schedule a retry, or dead-letter once retries are used up.

        Returns True when a retry was scheduled.
        """
        counter = self._counter_key(fields)
        attempt = int(await self.redis.incr(counter))
        await self.redis.expire(counter, RETRY_COUNTER_TTL_SECONDS)
        if attempt > self.max_retries:
            await self.dead_letter(msg_id, fields, error, attempts=attempt)
            return False

        delay = self.delay_for(attempt)
        member = json.dumps({"msg_id": msg_id, "fields": fields}, sort_keys=True)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.zadd(self.retry_key, {member: time.time() + delay})
            pipe.xack(self.stream, self.group, msg_id)
            await pipe.execute()
        logger.warning(
            "Retry %d/%d for %s in %.0fs: %s",
            attempt,
            self.max_retries,
            message_identity(fields),
            delay,
            error,
        )
        return True

    async def dead_letter(
        self, msg_id: str, fields: dict[str, str], reason: str, *, attempts: int = 0
    ) -> None:
        entry: dict[Any, Any] = {
            **fields,
            "original_msg_id": msg_id,
            "dlq_reason": reason[:1000],
            "attempts": str(attempts),
        }
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.xadd(self.dlq_stream, entry, maxlen=DLQ_MAXLEN, approximate=True)
            pipe.xack(self.stream, self.group, msg_id)
            pipe.delete(self._counter_key(fields))
            await pipe.execute()
        logger.error(
            "Dead-lettered %s onto %s after %d attempt(s): %s",
            message_identity(fields),
            self.dlq_stream,
            attempts,
            reason,
        )

    async def requeue_due(self, now: float | None = None) -> int:
        """Move retries whose back-off has elapsed back onto the stream."""
        # decode_responses=True: members come back as str.
        due = cast(
            list[str],
            await self.redis.zrangebyscore(
                self.retry_key,
                "-inf",
                time.time() if now is None else now,
                start=0,
                num=_REQUEUE_BATCH,
            ),
        )
        moved = 0
        for member in due:
            fields: dict[str, str] = json.loads(member)["fields"]
            flat = [part for pair in fields.items() for part in pair]
            if await self._requeue_one(keys=[self.retry_key, self.stream], args=[member, *flat]):
                moved += 1
        return moved
