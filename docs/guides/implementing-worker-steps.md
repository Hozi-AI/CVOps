# Implementing a Worker Service — Developer Handoff

**Who this is for:** the developer building one of the three worker services.

---

## What Already Exists

| Package | What it gives you |
|---|---|
| `packages/worker-common` | Consumer loop, job runner, DB session factory, storage client. You do not touch this. |
| `packages/steps/src/cvops_steps/` | The implemented steps (`extract_frames`, `export_yolo`, `train`, …). Use as reference. (The old `tools/frame-extractor/` CLI scripts were removed in c6c5b85 / 64460d7; specs archived in [`docs/archive/`](../archive/).) |
| `services/api/src/cvops_api/` | All ORM models, `StorageBackend`, `StepContext`, `GateException`, `emit_event`. Import freely. |

---

## The Three Worker Services to Build

| Service | Stream | Steps it runs |
|---|---|---|
| `services/worker-preprocessing` | `preprocessing` | `step.extract_frames`, `step.import_dataset`, `step.commit_dataset`, `step.export_yolo` (and any step with an empty `queue`) |
| `services/worker-cvat` | `cvat` | `step.human_review`, `step.deploy_model` |
| `services/worker-training` | `training` | `step.train`, `step.auto_label` |

> All three now exist. Stream = the step's `queue` attribute.

Each one is a separate directory under `services/`. This guide walks through building one — the pattern is identical for all three.

---

## Directory Structure (example: worker-preprocessing)

```
services/worker-preprocessing/
├── Dockerfile
├── pyproject.toml
└── src/
    └── worker_preprocessing/
        ├── __init__.py
        ├── main.py              ← entry point: starts the consumer loop
        └── steps/
            ├── __init__.py
            ├── extract_frames.py   ← step logic lives here
            └── commit_dataset.py
```

---

## 1. pyproject.toml

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "worker-preprocessing"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "cvops-worker-common",   # consumer loop, runner, session, storage
  "cvops-api",             # ORM models, StepContext, StorageBackend
  "opencv-python-headless>=4.9",
  "imagehash>=4.3",
  "pillow>=10.3",
]

[tool.hatch.build.targets.wheel]
packages = ["src/worker_preprocessing"]
```

Add only the deps your steps actually need. `worker-cvat` needs `httpx` for CVAT API calls. `worker-training` needs `docker`.

---

## 2. main.py — Entry Point

```python
# src/worker_preprocessing/main.py
import asyncio
import logging

from cvops_worker_common import ConsumerLoop
from cvops_api.core.registry import registry

from worker_preprocessing.steps.extract_frames import ExtractFramesStep
from worker_preprocessing.steps.commit_dataset import CommitDatasetStep

logging.basicConfig(level=logging.INFO)


def main() -> None:
    # Register this worker's steps so the runner can resolve them by type_key
    registry.register(ExtractFramesStep())
    registry.register(CommitDatasetStep())

    loop = ConsumerLoop(
        stream="preprocessing",
        step_types=["step.extract_frames", "step.commit_dataset"],
    )
    asyncio.run(loop.run_forever())


if __name__ == "__main__":
    main()
```

That's it for the entry point. `ConsumerLoop` handles everything else: reading from Redis, locking the job in PG, calling `step.run()`, writing status back, orphan recovery.

---

## 3. Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install shared packages first (layer cache)
COPY packages/worker-common ./packages/worker-common
COPY services/api ./services/api
RUN pip install --no-cache-dir \
    ./packages/worker-common \
    ./services/api

# Install this worker
COPY services/worker-preprocessing ./services/worker-preprocessing
RUN pip install --no-cache-dir ./services/worker-preprocessing

CMD ["python", "-m", "worker_preprocessing.main"]
```

---

## 4. Implementing a Step

Each step is a class with a `type_key` and a `run()` method. The `run()` method gets a `ctx` object that gives it everything it needs.

```python
# src/worker_preprocessing/steps/extract_frames.py

from cvops_api.engine.step import Step, StepContext
from cvops_api.db.models.samples import Sample


class ExtractFramesStep(Step):
    type_key = "step.extract_frames"

    async def run(self, ctx: StepContext, config: dict, inputs: dict) -> dict:
        source_id = inputs["source_id"]
        # ... your logic here ...
        return {"sample_ids": sample_ids}
```

### The `ctx` object — everything you need

```python
ctx.session      # AsyncSession — read/write PostgreSQL directly
ctx.storage      # StorageBackend — upload/download blobs from MinIO
ctx.project_id   # str UUID of the owning project
ctx.run_id       # str UUID of this step's runs row
ctx.emit_event   # callable — write to the audit log
```

### `ctx.storage` — blob operations

```python
# Upload bytes → deduplicates, stores in MinIO, inserts blobs row in PG
blob_hash = await ctx.storage.save_bytes(raw_bytes, "image/jpeg")
# returns "sha256:abc123..."

# Download bytes by hash
raw_bytes = await ctx.storage.get_bytes(blob_hash)
```

`save_bytes` replaces three things from the old tool: `upload_blob()` + `pg_register_blob()` + the MinIO client setup. One call does all of it.

### `ctx.session` — database operations

```python
from sqlalchemy import select
from cvops_api.db.models.samples import Sample

# Read
result = await ctx.session.execute(select(Sample).where(Sample.id == uuid))
sample = result.scalar_one_or_none()

# Write
sample = Sample(blob_hash=blob_hash, project_id=ctx.project_id, ...)
ctx.session.add(sample)
await ctx.session.flush()   # assigns sample.id without committing
str(sample.id)              # use this as output ref
```

**Never call `ctx.session.commit()`** — the runner owns the transaction and commits after `run()` returns.

### `ctx.emit_event` — audit log

```python
await ctx.emit_event(
    actor_id=ctx.actor_id,
    actor_type="service",
    entity_type="run",
    entity_id=ctx.run_id,
    action="extract_frames.completed",
    payload={"sample_count": len(sample_ids)},
)
```

---

## 5. Porting Logic from tools/

> Historical: `tools/frame-extractor/` has since been removed (c6c5b85, 64460d7) and `extract_frames` is ported — see `packages/steps/src/cvops_steps/extract_frames.py`. The mapping below is kept as a porting reference.

The working CLI logic lived in `tools/frame-extractor/`. Port the core algorithm, replace the I/O.

### extract_frames — mapping old to new

| Old tool (`tools/frame-extractor/extract_frames.py`) | New step |
|---|---|
| `build_minio_client()` + `upload_blob(minio_client, data, media_type)` | `await ctx.storage.save_bytes(data, media_type)` |
| `pg_register_blob(cur, ...)` | already done inside `save_bytes` |
| `pg_register_sample(cur, project_id, blob_hash, source_id, w, h, idx)` | `ctx.session.add(Sample(...)); await ctx.session.flush()` |
| `pg_conn.commit()` | **don't** — runner commits |
| `psycopg2.connect(DATABASE_URL)` | already handled — use `ctx.session` |
| `print(f"Uploaded {n} frames")` | `await ctx.emit_event(...)` |

The core OpenCV loop (`cv2.VideoCapture`, `cap.read()`, `cv2.imencode`) is **unchanged** — copy it directly.

---

## 6. The Full Step Chain — What Each Step Receives and Returns

```
step.extract_frames
  inputs:  { source_id: str }
  returns: { sample_ids: [str, ...] }

step.auto_label          (config: model_version_id required, confidence_threshold, ontology_id)
  inputs:  { sample_ids: [str, ...] }
  returns: { annotation_revision_ids: [str, ...] }

step.human_review  ← gate
  inputs:  { annotation_revision_ids: [str, ...] }
  raises:  GateException({"labeling_job_id": str})
  (after gate resolved, next step receives annotation_revision_ids)

step.commit_dataset
  inputs:  { sample_ids: [str, ...], annotation_revision_ids: [str, ...] }
  returns: { commit_id: str, ref_id: str }

step.export_yolo
  inputs:  { commit_id: str }
  returns: { export_blob_hash: "sha256:..." }

step.train
  inputs:  { export_blob_hash: "sha256:..." }
  returns: { model_version_id: str }
```

The executor wires these together automatically. Your step just reads from `inputs` and returns the next dict.

---

## 7. Gate Steps (worker-cvat only)

`step.human_review` is a gate — it parks the workflow instead of returning.

```python
from cvops_api.engine.step import Step, StepContext, GateException
from cvops_api.db.models.labeling import LabelingJob


class HumanReviewStep(Step):
    type_key = "step.human_review"
    is_gate = True

    async def run(self, ctx: StepContext, config: dict, inputs: dict) -> dict:
        annotation_revision_ids = inputs["annotation_revision_ids"]

        # push task to CVAT (see docs/services/worker-cvat.md for the full flow)
        cvat_task_id = await _push_to_cvat(ctx, annotation_revision_ids)

        # record the labeling job so the pull flow can find it later
        job = LabelingJob(
            run_id=ctx.run_id,
            cvat_task_id=cvat_task_id,
            status="pushed",
            annotation_revision_ids_in=annotation_revision_ids,
        )
        ctx.session.add(job)
        await ctx.session.flush()

        # park the workflow — do NOT return
        raise GateException({"labeling_job_id": str(job.id)})
```

The workflow stays in `status = waiting` until CVAT sends a webhook. The API receives the webhook and XADDs a `cvat_sync` doorbell; the CVAT worker pulls the annotations and calls `advance_workflow` in-process to resume (there is no `POST /internal/runs/{id}/advance` endpoint).

---

## 8. Available ORM Models

```python
from cvops_api.db.models.blobs import Blob
from cvops_api.db.models.samples import Sample
from cvops_api.db.models.annotations import AnnotationRevision
from cvops_api.db.models.labeling import LabelingJob
from cvops_api.db.models.versioning import Commit, CommitSample, Ref
from cvops_api.db.models.models import ModelVersion
```

---

## Reference Docs

| Document | Read when building |
|---|---|
| `docs/services/redis-streams.md` | stream names, message format, consumer group pattern |
| `docs/services/worker-preprocessing.md` | preprocessing worker full spec |
| `docs/services/worker-cvat.md` | cvat worker full spec, geometry conversion, export flow |
| `docs/services/worker-training.md` | training worker full spec, Docker ICD |
| `docs/services/step-contract.md` | step input/output table, queue assignments |
| `packages/worker-common/src/cvops_worker_common/` | what the base provides |
