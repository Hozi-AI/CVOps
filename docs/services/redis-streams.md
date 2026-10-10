# ICD — Redis Streams (Job Message Format)

**Owner:** Yehuda
**Last updated:** 2026-06-14

---

## Purpose

Redis Streams are the wake-up mechanism between the API and workers. They carry only a thin pointer to the job — all job state lives in PostgreSQL.

```
API creates runs row in PG  →  XADD thin message to Redis Stream
                                         ↓
                               Worker wakes up instantly
                                         ↓
                               Worker fetches full job from PG
                                         ↓
                               Worker executes, writes results to PG
                                         ↓
                               Worker calls advance_workflow (in-process)
                                         ↓
                               Coordinator enqueues next step → XADD next stream
                                         ↓
                               Worker XACKs message
```

---

## Stream Names

| Stream | Consumed by |
|---|---|
| `preprocessing` | worker-preprocessing |
| `cvat` | worker-cvat |
| `training` | worker-training |

---

## Message Format

All three streams use the same shape:

```json
{
  "job_id":    "<uuid — the runs row id>",
  "step_type": "<registered type_key, e.g. step.extract_frames>",
  "queue":     "<stream name>"
}
```

That is the entire message. Workers use `job_id` to fetch the full config and inputs from the `runs` table in PostgreSQL. Redis holds no job state.

---

## Producer (API)

The API's `advance_workflow` coordinator (`services/api/src/cvops_api/engine/coordinator.py`)
creates the child `runs` row, then — for each ready step — rings the queue:

```python
await redis.xadd(
    queue,                       # step.queue or "preprocessing" by default
    {
        "job_id":    str(child_run_id),
        "step_type": step.type_key,
        "queue":     queue,
    }
)
```

This is synchronous in-request (just row creation + `XADD`; no step runs in the API
process). The in-process `BackgroundTasks` executor has been removed.

---

## Consumer Pattern (Workers)

Each worker uses a named consumer group to allow multiple replicas without double-processing:

```python
# on startup — create group if not exists
await redis.xgroup_create(
    stream_name,
    groupname=f"worker-{stream_name}",
    id="$",           # only new messages from this point forward
    mkstream=True
)

# main loop
messages = await redis.xreadgroup(
    groupname=f"worker-{stream_name}",
    consumername=f"worker-{hostname}-{pid}",
    streams={stream_name: ">"},   # ">" = undelivered messages only
    count=1,
    block=5000                    # block up to 5s if stream is empty
)

for stream, msgs in messages:
    for msg_id, fields in msgs:
        job_id    = fields["job_id"]
        step_type = fields["step_type"]

        await process_job(job_id, step_type)

        await redis.xack(stream_name, f"worker-{stream_name}", msg_id)
```

---

## Auto-Chain Contract

> **Updated 2026-10-10.** `POST /internal/runs/{id}/advance` was never built.

Workers do not pick the next step themselves. After a step finishes, the worker calls
`advance_workflow(session, parent_run_id, actor)` from
`cvops_api.engine.coordinator` **in-process** (worker-preprocessing and worker-cvat
via `process_step`; the CVAT pull via `worker_cvat/sync.py`). The coordinator then:
1. Resolves the next ready steps in the workflow DAG
2. Creates their child `runs` rows in PG with frozen `input_refs`
3. Does `XADD` to each step's queue (`Step.queue`, empty → `preprocessing`)

All DAG logic stays in the coordinator; steps never know what comes next.

**Known gap:** worker-training uses `packages/worker-common`'s `JobRunner`, which
still POSTs to the non-existent advance endpoint (404, logged at debug). Steps on
the `training` queue (`train`, `auto_label`) therefore never advance their
workflow — tracked in #182.

Example ingest DAG (queues in brackets):

```
extract_frames (preprocessing) → human_review (cvat, gate)
    → commit_dataset (preprocessing) → export_yolo (preprocessing) → train (training)
```

---

## Orphan Recovery

Redis can lose messages on restart if persistence is not configured. PostgreSQL is the safety net.

Each worker runs this on startup and every 60 seconds:

```sql
SELECT id, step_type FROM runs
WHERE status = 'pending'
  AND step_type IN (<this worker's step types>)
  AND created_at < now() - interval '30 seconds'
```

For each result, re-enqueue into the Redis Stream. Consumer group deduplication prevents double-processing if the message is already in Redis.

---

## Why Not Fat Messages

The alternative — putting full config and inputs into the Redis message — creates a sync problem:

```
If PG and Redis disagree on job content → which is correct?
If the job is retried → Redis has stale config
If Redis loses the message → config is lost with it
```

Thin messages avoid all of this. PG is always the authority. Redis is just a doorbell.
