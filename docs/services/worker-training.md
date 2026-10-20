# ICD — Worker: Training

**Owner:** Nati / Yahav
**Last updated:** 2026-06-11

---

## What it is

Launches user-supplied Docker training containers via the ICD config, monitors them, reads results, and writes `model_version` records. This is the **only service with Docker socket access**.

---

## Dependencies

```
PostgreSQL   job pickup, training_containers ICD config, model_versions write
             — direct asyncpg connection, not through the API
MinIO        download export dataset, upload model weights + logs
             — direct S3/boto3 calls via StorageBackend abstraction, not through the API
Redis        consume from training stream
Docker       socket to launch and monitor user containers
```

**Important:** this worker connects to PostgreSQL and MinIO directly — it does not go through the API for data access. The `StorageBackend` abstraction wraps boto3 so the worker is storage-agnostic (MinIO, Garage, or S3 swap via config).

---

## Environment Variables

```
DATABASE_URL        postgresql+asyncpg://cvops:<password>@postgres:5432/cvops
MINIO_ENDPOINT      http://minio:9000
MINIO_ACCESS_KEY    <minio root user>
MINIO_SECRET_KEY    <minio root password>
REDIS_URL           redis://redis:6379/0
REDIS_STREAM        training
WORKER_TOKEN        <long-lived JWT>
DOCKER_TIMEOUT      7200    (seconds — max container run time, default 2h)
```

**Docker socket mount (docker-compose.yml):**
```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock
```
No other worker has this mount.

---

## Steps It Runs

| step_type | What it does |
|---|---|
| `step.train` | ICD-driven Docker dispatch, reads results, writes model_version |
| `step.auto_label` | Model inference over samples, writes annotation revisions (YOLO / LLM runners) |
| `step.evaluate` | Model evaluation on an eval commit (Phase 3) |

The worker doesn't keep a hand-written list: it calls `cvops_steps.register_all()`
(which also registers the model runners `auto_label` needs) and consumes, and
orphan-sweeps, every registered step whose `queue` is `training`. A step routed
to `training` later is picked up with no change here.

---

## Triggering a training run ("Train this commit")

Training is kicked off from the commit view (`CommitDetail` → **Train** dialog),
which POSTs to **`POST /api/v1/datasets/{id}/commits/{commit_id}/train`**
(`routers/datasets.py::train_commit`). The handler builds a one-node workflow
with a `step.train` node, creates the run, and calls `advance_workflow` — which
freezes the inputs and `XADD`s onto the **`training`** queue, where this worker
picks it up. It returns `{run_id}`; the UI navigates to the run view.

Two trainer modes (one `step.train` config either way):

- **Ad-hoc git trainer.** `{git_url, entry_point (default train.py), branch,
  hyperparams}` — the trainer repo is cloned and run against the exported
  dataset. Used in the dev loop with the bundled example trainer; no
  pre-registration needed.
- **Registered training container.** `{training_container_id, hyperparams}` —
  dispatches the project's registered ICD container (see **Training Container
  ICD** below).

In both modes the trainer logs to MLflow if it calls `mlflow.start_run()` and
emits `mlflow_run_id` in `metrics.json`; the worker stores it on
`model_versions.mlflow_run_id` and the model page surfaces the live **MLflow
run** link. See [`mlflow.md`](./mlflow.md) for the tracking-server side.

> Storage note: the `MINIO_*` names below are historical — the stack runs
> **Garage (S3)**; `StorageBackend` is storage-agnostic, so only the `S3_*` env
> (endpoint/key/bucket/region) is wired in compose.

---

## How Model Weights Are Written

```
1. Training container exits successfully
2. Worker tars the weights directory from {tmpdir}/output/weights/
3. worker calls ctx.storage.save_bytes(tar_bytes, "application/x-tar")
   └──► StorageBackend computes sha256 hash
   └──► uploads to MinIO at blobs/{hash[7:9]}/{hash[9:]}  (direct S3 PUT)
   └──► inserts blobs row in PG: {hash, storage_key, size_bytes}
   └──► returns weights_blob_hash = "sha256:<hex>"
4. worker inserts model_versions row in PG:
   {blob_hash: weights_blob_hash, trained_on_commit_id, metrics, ...}

MinIO holds the weight bytes. PG holds the reference.
```

---

## Training Flow

```
1. Load runs row → config: {training_container_id, hyperparams}
2. Load training_containers row → icd_config
3. Download export tar.gz from MinIO → extract to {tmpdir}/dataset/
4. Create {tmpdir}/output/
5. Build env dict from icd_config.inputs + hyperparams:
   e.g. DATASET_PATH={tmpdir}/dataset, EPOCHS=100, BATCH_SIZE=16
6. Build Docker volume mounts:
   {tmpdir}/dataset → icd_config.volume_mount  (read-only)
   {tmpdir}/output  → /output                  (read-write)
7. docker run:
   image:       training_containers.image
   environment: env dict
   volumes:     above
   detach:      True
   remove:      True
8. Poll container every 5s until exit
   Stream logs to MinIO as they arrive → update runs.logs_blob_hash
9. On exit code 0:
   a. Read {tmpdir}/output/{metrics_file_path} → parse JSON
   b. Extract mlflow_run_id from metrics if present
   c. Tar weights directory → upload to MinIO → weights_blob_hash
   d. INSERT model_versions:
      { blob_hash: weights_blob_hash,
        trained_on_commit_id: <from workflow run context>,
        training_container_id, hyperparams, metrics,
        mlflow_run_id: metrics.get('mlflow_run_id'),
        seed: hyperparams.get('seed') }
   e. UPDATE runs: { status: 'succeeded', output_refs: {model_version_id} }
10. On non-zero exit:
    UPDATE runs: { status: 'failed', error: last 500 chars of logs }
```

---

## MLflow Reference

If the training container reports to MLflow it should write the run ID into `metrics.json`:

```json
{
  "mAP50": 0.87,
  "loss":  0.043,
  "mlflow_run_id": "abc123def456"
}
```

This worker reads `mlflow_run_id` from metrics and stores it on `model_versions.mlflow_run_id`. The dashboard then shows a **"View in MLflow →"** link. No other MLflow coupling exists in CVOps.

---

## Training Container ICD (what user containers must speak)

```yaml
# registered once per project in training_containers table
image: my-org/my-trainer:latest

inputs:
  dataset_path: env: DATASET_PATH   # CVOps sets this to the extracted dataset path
  epochs:       env: EPOCHS
  batch_size:   env: BATCH_SIZE
  seed:         env: SEED

outputs:
  metrics_file: path: /output/metrics.json    # CVOps reads this on completion
  weights_path: path: /output/weights/        # CVOps tars and uploads this to MinIO

volume_mount: /data/dataset          # where the dataset is mounted inside the container
mlflow_tracking_uri: null            # optional — set to http://mlflow:5000 if used
```

CVOps does not care what is inside the container. Any framework, any architecture — as long as it reads env vars and writes to `/output/`.

---

## Reads From

| Source | What |
|---|---|
| PostgreSQL `runs` | Job config, input_refs |
| PostgreSQL `training_containers` | ICD config (env var mapping, volume mount, output paths) |
| MinIO | Export dataset tar.gz |

---

## Writes To

| Destination | What |
|---|---|
| PostgreSQL `model_versions` | Trained model record with metrics and commit link |
| PostgreSQL `runs` | Status, output_refs, logs_blob_hash |
| PostgreSQL `blobs` | Weights blob row, logs blob row |
| PostgreSQL `events` | Status transitions |
| MinIO | Model weights tar.gz, streaming training logs |
| Docker daemon | Container launch and lifecycle management |

---

## Does NOT

```
✗ talk to CVAT
✗ process or transform data items
✗ implement any training logic — that lives entirely in the user's container
✗ know what model architecture is training
✗ have access to user source data — only the exported dataset tar.gz
```
