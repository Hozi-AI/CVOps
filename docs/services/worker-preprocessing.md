# ICD — Worker: Preprocessing

**Owner:** Nati / Yahav
**Last updated:** 2026-06-14

---

## What it is

Executes data ingestion and dataset commit steps: frame extraction and dataset commit. Runs any registered step with `queue = "preprocessing"`. Has no knowledge of CVAT, model inference, Docker containers, or any specific data domain — it just resolves a `step_type` from the registry and calls `step.run()`.

---

## Base Package

Built on `packages/worker-common` — the shared worker library that provides:

```
ConsumerLoop       XREADGROUP loop, XACK, orphan recovery, graceful shutdown
SessionFactory     async SQLAlchemy session factory (direct asyncpg, not through the API)
StorageBackend     MinIO/S3 abstraction (same interface as the API's get_storage())
WorkerRegistry     resolves step_type → Step instance (same registry as the API)
```

Every worker imports `worker-common` and specialises it with its own step implementations and stream name. No worker re-implements the consumer loop or DB session setup.

---

## Dependencies

```
PostgreSQL   job pickup and result write-back — direct asyncpg connection, not through the API
MinIO        read source blobs, write output blobs — direct S3/boto3 via StorageBackend
Redis        consume from preprocessing stream, orphan recovery
```

Workers do not go through the API for data access. The API is never in the data path for workers.

---

## Environment Variables

```
DATABASE_URL        postgresql+asyncpg://cvops:<password>@postgres:5432/cvops
MINIO_ENDPOINT      http://minio:9000
MINIO_ACCESS_KEY    <minio root user>
MINIO_SECRET_KEY    <minio root password>
REDIS_URL           redis://redis:6379/0
REDIS_STREAM        preprocessing
WORKER_TOKEN        (unused — this worker makes no API calls; the API doesn't validate it, #71)
WORKER_CONCURRENCY  4    (optional — parallel job slots, default 4)
```

---

## Steps It Runs

| step_type | queue | What it does |
|---|---|---|
| `step.extract_frames` | `preprocessing` | OpenCV frame extraction, exact-hash dedup, thumbnail generation |
| `step.import_dataset` | `preprocessing` | Ingest a YOLO / COCO / raw dataset ([doc 16](../16-dataset-import.md)) |
| `step.commit_dataset` | `preprocessing` | Creates immutable commit + CAS branch advance |
| `step.export_yolo` | `preprocessing` | Materialise a commit as a YOLO dataset |
| `step.chunk_text`, `step.parse_sensor`, `step.export_jsonl`, `step.export_csv` | `preprocessing` | Non-CV modality steps |

`step.human_review` runs on the CVAT worker and `step.auto_label` / `step.train` on worker-training (`training` queue). `step.export_yolo` has no queue override, so it runs here on `preprocessing`.

---

## How Blobs Are Written

Every byte the worker produces (frames, thumbnails) follows this exact pattern:

```
1. worker calls ctx.storage.save_bytes(raw_bytes, media_type)
   └──► StorageBackend computes sha256 hash of bytes
   └──► uploads to MinIO at path blobs/{hash[0:2]}/{hash[2:]}  (direct S3 PUT)
   └──► inserts blobs row in PG: {hash, storage_key, size_bytes, media_type}
   └──► returns blob_hash = "sha256:<hex>"

2. worker inserts samples row in PG:
   {blob_hash: "sha256:...", source_id, metadata}

MinIO holds the bytes. PG holds the reference (hash → storage key).
They are linked by the content hash.
```

---

## Job Pickup Flow

```
1. XREADGROUP on Redis Stream "preprocessing"
   → receives {job_id, step_type, queue}

2. SELECT * FROM runs WHERE id = job_id FOR UPDATE SKIP LOCKED
   → fetches full config + input_refs from PG

3. UPDATE runs SET status = 'running', started_at = now()

4. step = registry.resolve(step_type)
   await step.run(ctx, config, inputs)

5. On success:
   UPDATE runs SET status = 'succeeded', output_refs = {...}, finished_at = now()
   INSERT events row
   XACK message on Redis Stream
   advance_workflow(session, parent_run_id) — in-process, via process_step
     → coordinator enqueues the next ready DAG steps to their streams

6. On failure:
   UPDATE runs SET status = 'failed', error = <message>, finished_at = now()
   INSERT events row
   XACK message (do not requeue — retry is user-triggered from dashboard)
```

---

## Auto-Chain

> **Status (2026-10-10):** `POST /internal/runs/{id}/advance` was never built. Workers advance the DAG **in-process** by calling `advance_workflow` (`services/api/src/cvops_api/engine/coordinator.py`): worker-preprocessing via the engine's `process_step`, worker-cvat via `sync.py`. The `packages/worker-common` runner used by worker-training still POSTs to the missing endpoint, so workflows stall after `step.train` / `step.auto_label` — tracked as #182.

```
preprocessing completes a step (process_step)
    → advance_workflow(session, parent_run_id)   — in-process
    → coordinator XADDs whatever the DAG makes ready
      (e.g. extract_frames → human_review on cvat; commit_dataset → export_yolo on preprocessing)
```

---

## Orphan Recovery

On startup and every 60 seconds, query for jobs that are pending in PG but not in Redis (handles Redis restart / message loss):

```sql
SELECT id FROM runs
WHERE status = 'pending'
  AND step_type IN ('step.extract_frames', 'step.commit_dataset')
  AND created_at < now() - interval '30 seconds'
```

Re-enqueue any found rows into the `preprocessing` Redis Stream.

---

## Scaling

```yaml
worker-preprocessing:
  deploy:
    replicas: 2   # SELECT FOR UPDATE SKIP LOCKED handles concurrency safely
```

---

## Reads From

| Source | What |
|---|---|
| PostgreSQL `runs` | Job config, input_refs |
| PostgreSQL `samples` | Sample metadata for commit step |
| PostgreSQL `annotation_revisions` | Revision IDs for commit step |
| MinIO | Source blobs (video files, uploaded images) |

---

## Writes To

| Destination | What |
|---|---|
| PostgreSQL `runs` | Status updates, output_refs, finished_at |
| PostgreSQL `samples` | New rows on ingest |
| PostgreSQL `blobs` | New content-addressed blob rows |
| PostgreSQL `commits` + `commit_samples` | Immutable dataset snapshots |
| PostgreSQL `events` | One row per status transition |
| MinIO | Frame JPEGs, thumbnail PNGs |
| `advance_workflow` (in-process) | Workflow advance — enqueues the next ready steps |

---

## Does NOT

```
✗ run model inference or auto-labeling
✗ talk to CVAT
✗ export datasets
✗ launch Docker containers
✗ hold the Docker socket
✗ proxy bytes through the API (writes directly to PG and MinIO)
✗ know what domain the data is in (CV, RF, audio — domain-agnostic)
```
