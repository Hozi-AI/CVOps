# ICD — API Service

**Owner:** Yehuda
**Last updated:** 2026-06-11

---

## What it is

The orchestration layer. Handles all HTTP from the browser, manages auth, owns all CRUD, drives the workflow coordinator (`advance_workflow` creates child runs and `XADD`s them to Redis Streams; steps run out-of-process in the workers — see [redis-streams.md](./redis-streams.md)), and issues presigned URLs for Garage (S3) access.

---

## Dependencies (must be healthy before API starts)

```
PostgreSQL   all reads and writes
Garage (S3)  presigned URL generation only — API never touches bytes
Redis        JWT revocation list, cache, SSE, job enqueue (XADD)
```

---

## Environment Variables

```
DATABASE_URL        postgresql+asyncpg://cvops:<password>@postgres:5432/cvops
S3_ENDPOINT         http://garage:3900       # internal; presigned URLs use S3_PUBLIC_ENDPOINT or <request host>:S3_PUBLIC_PORT
S3_ACCESS_KEY       <Garage key, GK…>
S3_SECRET_KEY       <Garage secret>
S3_BUCKET           cvops-blobs
REDIS_URL           redis://redis:6379/0
JWT_SECRET          <min 32 chars, random>
WORKER_TOKEN        <shared secret; sent as bearer to the model deployer — not validated on inbound calls (#71)>
```

---

## Reads From

| Source | What |
|---|---|
| PostgreSQL | All tables — auth, projects, data_items, commits, runs, events, etc. |
| Redis | Distributed locks (branch CAS protection), short-lived presigned URL cache |

---

## Writes To

| Destination | What |
|---|---|
| PostgreSQL | All domain tables. Every mutation emits an `events` row. |
| Redis Streams | `preprocessing`, `labeling`, `training` — thin `{job_id, step_type, queue}` messages when a run is created (Phase 2 only) |
| Redis pub/sub | `runs:{run_id}` channel — event payloads for SSE delivery to browser |

---

## Exposes

```
REST API     /api/v1/*                           all endpoints (see MASTER_PLAN §12, README API table)
SSE stream   /api/v1/runs/{id}/events/stream     live run event push
Webhook      /api/v1/internal/cvat/webhook       CVAT completion signal (HMAC, CVAT_WEBHOOK_SECRET)
Health       /api/v1/internal/health             {status} — DB check only
Liveness     /health                             root, unversioned
```

---

## Execution

> **Updated 2026-10-10.** The in-process `BackgroundTasks` executor ("Phase 1") and `engine/executor.py` are gone. Steps always run out-of-process.

The API creates a `pending` parent run and calls `advance_workflow` (`engine/coordinator.py`) in-request: it creates a child `runs` row per ready step, freezes its resolved inputs, and `XADD`s a thin `{job_id, step_type, queue}` message to the step's Redis Stream. A per-queue worker claims the child, runs it via `process_step`, and calls `advance_workflow` again. The API never waits for completion. See [redis-streams.md](./redis-streams.md).

---

## Does NOT

```
✗ proxy image bytes, model weights, or export archives — issues presigned URLs instead
✗ execute long-running step logic in the request thread (Phase 2)
✗ hold the Docker socket
✗ talk to CVAT directly
✗ know what a "frame", "RF capture", or "audio segment" is — domain-agnostic
```
