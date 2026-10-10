# ICD — Worker: CVAT

**Owner:** TBD
**Last updated:** 2026-06-16

---

## What it is

The `cvat`-queue worker. After the origin/dev integration it is a **dual-role**
worker — one host process / container that consumes the `cvat` Redis stream and
also serves a small HTTP API on **:8001**:

1. **Human-in-the-loop review (sync role).** Runs `step.human_review`: pushes a
   review batch into CVAT, parks the run at the gate, and — on a CVAT completion
   doorbell — pulls reviewed annotations back into `annotation_revisions` and
   resumes the gated run. See **Push Flow** / **Pull Flow** below.
2. **Model deployment to CVAT (deploy/export role).** Runs `step.deploy_model`
   and serves `POST /deploy`, `GET /models`, `DELETE /models/{id}` on :8001 —
   deploying a trained `.pt` model to Nuclio so it appears as a CVAT auto-label
   function. The API's `cvat.py` router proxies the dashboard to these endpoints
   via `MODEL_DEPLOYER_URL → worker-cvat:8001`. See **Model deployment to CVAT**.

> **Architecture note.** The worker no longer uses `worker-common`'s
> `ConsumerLoop`; it runs its own consumer-group loop (`worker_cvat/worker.py`,
> entry point `python -m worker_cvat`). Each `cvat` message is routed by shape:
> a `{kind: "cvat_sync", cvat_task_id}` doorbell → the pull handler
> (`handle_cvat_sync`); a `{job_id, step_type}` doorbell → `process_step`
> (covers both `step.human_review` and `step.deploy_model`). Postgres is the
> authority; Redis is only the doorbell. Storage is **Garage (S3)**, not MinIO.
> Several sections below predate this and describe the original aspirational
> design (auto_label / export_yolo as cvat steps, MinIO, advance via
> `POST /internal`) — kept for history; the **Model deployment to CVAT** and
> **HTTP endpoints** sections reflect current reality.

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
PostgreSQL   job pickup, labeling_jobs, annotation_revisions, blobs — direct asyncpg
MinIO        read images for auto-label, write export artifacts — direct S3/boto3 via StorageBackend
Redis        consume from cvat stream
CVAT         REST API — push tasks, upload images, pull completed annotations
(no API call) chaining is in-process: `process_step` / `handle_cvat_sync` call
             `advance_workflow` from `cvops_api.engine.coordinator` directly.
             There is no `POST /internal/runs/{id}/advance` endpoint.
```

---

## Environment Variables

```
# Core
DATABASE_URL          postgresql+asyncpg://cvops:<password>@postgres:5432/cvops
REDIS_URL             redis://redis:6379/0
REDIS_STREAM          cvat
S3_ENDPOINT           http://garage:3900          # Garage (S3), not MinIO
S3_ACCESS_KEY / S3_SECRET_KEY / S3_BUCKET / S3_REGION
WORKER_TOKEN          <shared secret; not validated by the API (#71) — this worker
                       makes no API calls>

# Sync role (review push/pull — read by cvops_cvat_client)
CVAT_URL              http://cvat_server:8080     # falls back to CVAT_HOST
CVAT_PUBLIC_URL       http://localhost:8080       # browser-reachable "Open in CVAT" base
CVAT_USERNAME / CVAT_PASSWORD
CVAT_WEBHOOK_SECRET   <secret CVAT signs completion webhooks with>

# Deploy role (model deployment — read by the deployer / cvat_client)
CVAT_HOST             http://cvat_server:8080
MODEL_DEPLOYER_PORT   8001                        # HTTP API port
NUCTL_PATH            /abs/path/to/services/worker-cvat/nuctl
```

---

## Steps It Runs

| step_type | queue | What it does |
|---|---|---|
| `step.human_review` | `cvat` | Gate step — push a batch to CVAT for human review, run → `waiting`; resumed by a `cvat_sync` doorbell. **Implemented.** |
| `step.deploy_model` | `cvat` | Deploy a trained `.pt` model to Nuclio as a CVAT auto-label function. **Implemented** (registered locally by the worker via `DeployModelStep`). |
| `step.auto_label` | `training` | Model inference writing model-sourced `annotation_revisions`. **Implemented** in `cvops_steps` (local YOLO runner), but it routes to the `training` queue — **not** a cvat-worker step. |
| `step.export_yolo` | `preprocessing` | Materialise a committed dataset to YOLO. Runs in `packages/steps`, dispatched on the `preprocessing` queue — **not** a cvat-worker step. |

`step.human_review` comes from `cvops_steps` (registered via `register_all()`);
`step.deploy_model` is registered locally by the worker. Both route to the
`cvat` queue (`Step.queue == "cvat"`), which is why their doorbells land here.

---

## Auto-Label Flow (step.auto_label)

> **Historical.** `step.auto_label` runs on the `training` queue (worker-training), not here, and takes `sample_ids`. The flow below is the original design.

Triggered via auto-chain from the preprocessing worker after `step.extract_frames` completes.

```
1. Load data_item_ids from input_refs
2. For each data_item: fetch image blob from MinIO via presigned GET URL
3. Run inference (model config from step config, e.g. model_version_id or external endpoint)
4. Convert predictions → canonical annotation payload
5. INSERT annotation_revisions rows:
   { annotation_type: 'annotation.cv.detection',
     source: 'model',
     review_status: 'pending',
     data_item_id, payload }
6. UPDATE runs: { status: 'succeeded', output_refs: {annotation_revision_ids} }
7. INSERT events row
8. XACK Redis message
9. POST /internal/runs/{workflow_run_id}/advance
   { step_run_id, output_refs: {annotation_revision_ids} }
   → executor enqueues step.human_review to cvat stream
```

---

## Entry Point (starting a review)

A review is kicked off from the dataset page: **`POST /datasets/{id}/review`**
(`services/api/.../routers/datasets.py`). It resolves the review set from the
dataset's current commit — the `main` branch ref, falling back to the newest
commit — reading `commit_samples` for the sample ids and their pre-label
`annotation_revision_id`s. It find-or-creates the project's `human_review`
workflow (a fixed single `step.human_review` node whose inputs are
`$run.params.sample_ids` / `$run.params.annotation_revision_ids`), dispatches a
run via `create_workflow_run` + `advance_workflow`, and returns `{run_id}`. The
frontend navigates to the run view, where the gate banner renders "Open in CVAT"
once this worker pushes the task. 400 if the dataset has no committed samples.

---

## Push Flow (step.human_review)

```
1. Load annotation_revision_ids from input_refs
2. Load data_items for those revisions
3. Call CVAT API:
   a. GET  /api/projects?name={cvops_project_name}  find or create CVAT project
   b. POST /api/tasks                               create task for this batch
   c. POST /api/tasks/{id}/data                     upload images
   d. POST /api/tasks/{id}/annotations              upload pre-labels (auto-label output)
4. INSERT labeling_jobs row:
   { status: 'pushed', cvat_task_id, cvat_job_ids,
     annotation_revision_ids_in, sample_count }
5. POST /api/webhooks on CVAT to register completion callback
6. Raise GateException({labeling_job_id})
   → executor sets run status = 'waiting'
   → dashboard shows "N jobs pending — Open in CVAT →"
```

---

## Pull Flow (on CVAT completion)

Triggered by webhook (primary) or polling fallback every `CVAT_POLL_INTERVAL` seconds.

The API receives `POST /internal/cvat/webhook`, validates `CVAT_WEBHOOK_SECRET`, and enqueues a sync message into the `cvat` Redis Stream. This worker picks it up.

```
1. Check all CVAT jobs in labeling_jobs.cvat_job_ids are completed
2. GET /api/tasks/{cvat_task_id}/annotations   download reviewed annotations
3. Convert CVAT geometry → canonical annotation payload (see Geometry Conversion)
4. INSERT annotation_revisions rows:
   { annotation_type: 'annotation.cv.detection',
     source: 'human',
     review_status: 'accepted',
     author_user_id: <mapped from CVAT user> }
5. UPDATE labeling_jobs:
   { status: 'completed', completed_at: now(),
     annotation_revision_ids_out: [...] }
6. UPDATE runs: { status: 'succeeded', output_refs: {annotation_revision_ids} }
7. INSERT events row
8. XACK Redis message
9. advance_workflow(session, parent_run_id) — in-process (worker_cvat/sync.py)
   → enqueues step.commit_dataset to the preprocessing stream
```

Idempotent — if pull flow fires twice (webhook + poll race), `labeling_jobs.status == 'completed'` at step 1 short-circuits the second run.

---

## Model deployment to CVAT (export-to-CVAT)

"Export to CVAT" = take a trained model and make it available **inside** CVAT as
an auto-label function, so a reviewer can pre-annotate new tasks with it. This is
the deploy role; it does not export a dataset.

Two ways in, both ending at the worker's `/deploy` endpoint:

- **Dashboard / API.** `POST /api/v1/models/{id}/cvat-deploy` (`routers/cvat.py`)
  fetches the `ModelVersion`'s `.pt` weights and POSTs them to
  `${MODEL_DEPLOYER_URL}/deploy` (→ `worker-cvat:8001`).
- **Workflow.** `step.deploy_model` (`DeployModelStep`, queue `cvat`) takes a
  `model_version_id` input + `model_name` config, downloads the weights from
  storage, and calls the same deployer in-process.

Deployer flow (`worker_cvat/deployer.py`, host process shells out to `nuctl`):

```
1. ultralytics.YOLO(pt) → extract class labels from the model
2. Render a Nuclio function.yaml (YOLO_BASE_IMAGE, labels, CVAT serializer)
3. nuctl deploy <func> --platform local
     --platform-config {attributes:{network: CVAT_NETWORK}}   # join cvat_cvat net
4. Return the Nuclio function name → CVAT lists it under "Models" / auto-annotate
```

`GET /models` lists deployed Nuclio functions (CVAT SDK); `DELETE /models/{id}`
tears one down (`nuctl delete function`). Requires the Docker socket and `nuctl`
on PATH (`NUCTL_PATH`); under Tilt the `nuctl-install` + `docker-socket-perms`
resources provide them. Nuclio itself is profile-gated (`app`/`all`) — bring up
`docker compose --profile app` for the full deploy path.

---

## HTTP endpoints (:8001)

Served by a small FastAPI app inside the worker (`MODEL_DEPLOYER_PORT`, default
8001). The API's `cvat.py` router is the only client, proxying via
`MODEL_DEPLOYER_URL → http://worker-cvat:8001`.

| Method & path | Purpose |
|---|---|
| `POST /deploy` (multipart: `model_name`, `file`) | Deploy a `.pt` to Nuclio; returns `{function_name}` |
| `GET /models` | List deployed Nuclio/CVAT auto-label functions |
| `DELETE /models/{function_id}` | Remove a deployed function |

---

## Export Flow (step.export_yolo)

> **Historical.** `step.export_yolo` runs on the `preprocessing` queue, not here. The flow below is the original design.

Triggered via auto-chain from the preprocessing worker after `step.commit_dataset` completes.

```
1. Load commit_id from input_refs
2. SELECT commit_samples JOIN samples JOIN blobs WHERE commit_id = ...
3. For each sample: presigned GET URL → download image bytes from MinIO
4. Build YOLO directory layout:
   images/train/  images/val/  labels/train/  labels/val/
5. Write annotation_revisions as YOLO .txt label files
   (one file per image: class_id cx cy w h per line, normalised)
6. Tar the directory → ctx.storage.save_bytes(tar_bytes, "application/x-tar")
   → StorageBackend: sha256 hash, upload to MinIO, INSERT blobs row in PG
7. UPDATE runs: { status: 'succeeded', output_refs: {export_blob_hash} }
8. INSERT events row
9. XACK Redis message
10. POST /internal/runs/{workflow_run_id}/advance
    { step_run_id, output_refs: {export_blob_hash} }
    → executor enqueues step.train to training stream
```

---

## Auto-Chain Summary

There is no fixed chain — the workflow DAG decides what comes next. After any
step finishes, whoever ran it calls `advance_workflow`, which creates the child
`runs` row for every newly ready step and `XADD`s it to that step's queue:

- `worker-preprocessing` and this worker run steps through `process_step`, which
  advances in-process; the CVAT pull (`handle_cvat_sync`) also advances in-process.
- `worker-training` uses `worker-common`'s `JobRunner`, which instead POSTs to
  `/internal/runs/{id}/advance`. That endpoint does not exist (404, logged at
  debug), so steps on the `training` queue (`train`, `auto_label`) never advance
  their workflow — tracked in #182.

---

## Geometry Conversion

**Push — CVOps → CVAT:**
```
bbox [cx, cy, w, h] normalised →
  x1 = (cx - w/2) * W
  y1 = (cy - h/2) * H
  x2 = (cx + w/2) * W
  y2 = (cy + h/2) * H
  (W, H = data_item width, height from metadata)
```

**Pull — CVAT → CVOps:**
```
CVAT [x1, y1, x2, y2] absolute pixels →
  cx = (x1 + x2) / (2 * W)
  cy = (y1 + y2) / (2 * H)
  w  = (x2 - x1) / W
  h  = (y2 - y1) / H
```

---

## Reads From

| Source | What |
|---|---|
| PostgreSQL `runs` | Job config, input_refs |
| PostgreSQL `annotation_revisions` | Pre-labels to push to CVAT |
| PostgreSQL `data_items` | Image metadata (width, height) for geometry conversion |
| PostgreSQL `commits` + `commit_samples` | Sample membership for YOLO export |
| MinIO | Image bytes for auto-label inference and YOLO export |
| CVAT REST API | Completed annotation data |

---

## Writes To

| Destination | What |
|---|---|
| PostgreSQL `annotation_revisions` | Model-generated and human-reviewed revisions |
| PostgreSQL `labeling_jobs` | Push record, completion record |
| PostgreSQL `runs` | Status updates, output_refs |
| PostgreSQL `blobs` | Export tar.gz blob row |
| PostgreSQL `events` | Status transitions |
| MinIO | YOLO export tar.gz archive |
| CVAT REST API | Tasks, images, pre-labels |
| Redis `XADD` (via in-process `advance_workflow`) | Next ready steps' doorbells |

---

## Does NOT

```
✗ extract frames from video
✗ create dataset commits
✗ launch Docker containers
✗ hold the Docker socket
✗ proxy bytes through the API (reads PG and MinIO directly)
```
