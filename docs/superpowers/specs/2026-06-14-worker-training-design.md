# Design Spec — services/worker-training

**Date:** 2026-06-14
**Status:** DONE

---

## Overview

`services/worker-training` is a self-contained Python worker service that consumes jobs from the `training` Redis Stream, launches user-supplied Docker training containers, captures results, and writes a `model_version` record to PostgreSQL. It is the only service with Docker socket access.

---

## What Already Exists

| Component | Location | Role |
|---|---|---|
| Worker infrastructure | `packages/worker-common` | ConsumerLoop, JobRunner, session factory, StorageBackend |
| Step contract | `services/api/src/cvops_api/engine/step.py` | `Step`, `StepContext`, `GateException` |
| ORM models | `services/api/src/cvops_api/db/models/` | `Run`, `ModelVersion`, `TrainingContainer`, `Blob` |
| Storage abstraction | `services/api/src/cvops_api/core/storage.py` | `S3Backend.save_bytes()`, `get_bytes()` |
| ICD reference | `docs/services/worker-training.md` | Full spec for this worker |
| Handoff guide | `docs/guides/implementing-worker-steps.md` | How to build a worker service on worker-common |

---

## Directory Structure

```
services/worker-training/
├── Dockerfile
├── pyproject.toml
└── src/
    └── worker_training/
        ├── __init__.py
        ├── main.py          ← entry point: register step, start ConsumerLoop
        └── steps/
            ├── __init__.py
            └── train.py     ← TrainStep implementation
```

---

## pyproject.toml

```toml
[project]
name = "worker-training"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "cvops-worker-common",
  "cvops-api",
  "docker>=7.1",
]
```

---

## main.py

```python
from cvops_worker_common import ConsumerLoop
from cvops_api.core.registry import registry
from worker_training.steps.train import TrainStep

def main():
    registry.register(TrainStep())
    loop = ConsumerLoop(
        stream="training",
        step_types=["step.train"],
    )
    asyncio.run(loop.run_forever())
```

---

## step.train — Full Flow

**Inputs:** `{ export_blob_hash: "sha256:..." }`
**Returns:** `{ model_version_id: str }`

### Step-by-step

```
1. Load config from runs row:
     { training_container_id: uuid, hyperparams: {epochs, batch_size, seed, ...} }

2. Load TrainingContainer row from PG:
     { image: str, icd_config: {inputs, outputs, volume_mount, mlflow_tracking_uri} }

3. Download export dataset:
     bytes = await ctx.storage.get_bytes(export_blob_hash)
     extract tar.gz → /tmp/{run_id}/dataset/

4. Create output directory:
     mkdir /tmp/{run_id}/output/

5. Build Docker env dict from ICD inputs + hyperparams:
     e.g. DATASET_PATH=/tmp/{run_id}/dataset, EPOCHS=100, BATCH_SIZE=16, SEED=42

6. docker.containers.run(
       image=training_container.image,
       environment=env_dict,
       volumes={
           "/tmp/{run_id}/dataset": {"bind": icd_config.volume_mount, "mode": "ro"},
           "/tmp/{run_id}/output":  {"bind": "/output", "mode": "rw"},
       },
       detach=True,
       remove=True,
   )

7. Poll container.wait() — blocks until exit (docker-py handles timeout via DOCKER_TIMEOUT env var)

8a. Exit code 0 (success):
     - Read /tmp/{run_id}/output/metrics.json → parse JSON
     - Extract mlflow_run_id from metrics if present
     - Tar /tmp/{run_id}/output/weights/
     - weights_blob_hash = await ctx.storage.save_bytes(tar_bytes, "application/x-tar")
     - INSERT model_versions:
         { project_id, blob_hash: weights_blob_hash,
           trained_on_commit_id: resolved from run context,
           training_container_id, hyperparams, metrics,
           mlflow_run_id: metrics.get("mlflow_run_id") }
     - return { model_version_id: str(model_version.id) }

8b. Non-zero exit (failure):
     - Read last 500 chars of container logs
     - raise RuntimeError(logs) → runner marks run failed

9. Finally (always):
     - shutil.rmtree(/tmp/{run_id}/, ignore_errors=True)
```

---

## Concurrency Model

- `WORKER_CONCURRENCY = 1` — one job per worker process
- Scale by adding replicas in docker-compose, not by increasing concurrency
- `SELECT FOR UPDATE SKIP LOCKED` in the runner handles multiple replicas safely

---

## MLflow Integration

User-owned. The training container reports to MLflow independently. CVOps only:
- Reads `mlflow_run_id` from `metrics.json` if present
- Stores it on `model_versions.mlflow_run_id`
- Dashboard shows "View in MLflow →" link for runs that have it

No MLflow SDK in this service. No metrics replication.

---

## Error Handling

| Scenario | Behaviour |
|---|---|
| Container non-zero exit | `run.status = failed`, `run.error = last 500 chars of logs` |
| Container timeout | Kill container, fail run with timeout message |
| Missing metrics.json | Fail run — container must write this file |
| Missing weights dir | Fail run — container must write weights |
| MinIO upload error | Exception propagates → runner marks failed |
| tmpdir cleanup failure | Log warning, do not fail the run |

No automatic retries. Retry is manual from the dashboard.

---

## Environment Variables

```
DATABASE_URL         postgresql+asyncpg://cvops:<password>@postgres:5432/cvops
MINIO_ENDPOINT       http://minio:9000
MINIO_ACCESS_KEY     <minio root user>
MINIO_SECRET_KEY     <minio root password>
REDIS_URL            redis://redis:6379/0
REDIS_STREAM         training
WORKER_TOKEN         <long-lived JWT>
DOCKER_TIMEOUT       7200    (seconds, default 2h)
API_BASE_URL         http://api:8000
```

---

## Dockerfile

```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY packages/worker-common ./packages/worker-common
COPY services/api ./services/api
RUN pip install --no-cache-dir ./packages/worker-common ./services/api

COPY services/worker-training ./services/worker-training
RUN pip install --no-cache-dir ./services/worker-training

CMD ["python", "-m", "worker_training.main"]
```

Docker socket mount in compose:
```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock
```

---

## Does NOT

```
✗ implement any training logic
✗ know what model architecture is training
✗ touch CVAT
✗ handle preprocessing or annotation
✗ retry automatically
✗ integrate with MLflow SDK directly
```
