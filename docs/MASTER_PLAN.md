
# CVOps — Master Plan

**Status:** Living reference. Supersedes docs 01–10. Last updated: 2026-06-11.
**Audience:** Full team — Yehuda (substrate/versioning/orchestration), Nati/Yahav (steps: extract/label/export/train), Itai (executor).

> **Status note (2026-10-10).** Parts of this plan diverged from what shipped:
> - The `samples` → `data_items` rename was **never done** — the table is still `samples`, made domain-agnostic with a `modality` field instead (migration `0007_multi_modality`).
> - Steps run out-of-process on **Redis Streams** + the PG `runs` table — no Celery, and no in-process `BackgroundTasks` executor. See [services/redis-streams.md](./services/redis-streams.md).
> - `worker-labeling` shipped as **`worker-cvat`**.
> - Current priorities and ordering live in [ROADMAP.md](../ROADMAP.md), not §19.

> **Rev 2026-06-11 — Architecture decisions locked in this revision:**
> 1. `samples` table renamed to `data_items` with `item_type` + `metadata JSONB` — platform is now domain-agnostic (supports CV, RF, audio, etc.)
> 2. Celery removed — replaced with Redis Streams (job wake-up) + PostgreSQL `runs` table (job state + queue). PG is source of truth; Redis is the fast signal.
> 3. Single `worker` service split into three separate Docker services: `worker-preprocessing`, `worker-labeling`, `worker-training`. Docker socket moved to `worker-training` only.
> 4. Redis Stream message format locked: thin message `{job_id, step_type, queue}` — workers fetch full job details from PG.
> 5. New registry categories added: `annotation_type`, `thumbnail_generator`, `source_type`.

---

## Table of Contents

1. [What CVOps Is](#1-what-cvops-is)
2. [Non-Negotiable Invariants](#2-non-negotiable-invariants)
3. [Nine Cross-Cutting Principles](#3-nine-cross-cutting-principles)
4. [Technology Stack](#4-technology-stack)
5. [Complete Data Model](#5-complete-data-model)
6. [Versioning, Concurrency, and Merge](#6-versioning-concurrency-and-merge)
7. [Storage and Blob Access](#7-storage-and-blob-access)
8. [Registry — All Registered Types](#8-registry--all-registered-types)
9. [Step Contract and Executor](#9-step-contract-and-executor)
10. [Step Implementations](#10-step-implementations)
11. [CVAT Integration](#11-cvat-integration)
12. [Complete API Surface](#12-complete-api-surface)
13. [Auth Flow](#13-auth-flow)
14. [Frontend Architecture](#14-frontend-architecture)
15. [Repository Structure](#15-repository-structure)
16. [Docker Compose Service Map](#16-docker-compose-service-map)
17. [Testing Strategy](#17-testing-strategy)
18. [Resolved Design Decisions](#18-resolved-design-decisions)
19. [Phased Build Plan](#19-phased-build-plan)
20. [Team Task Assignment](#20-team-task-assignment)
21. [Glossary](#21-glossary)

---

## 1. What CVOps Is

CVOps is a model-agnostic ML lifecycle dashboard. It sits between raw data and a trained model: it handles video/image ingestion, frame extraction, automated labeling, human annotation via CVAT, dataset versioning (git-semantics: commits/branches/tags), and training dispatch to user-supplied Docker containers.

The user brings raw video or images and a Docker-containerized training script. CVOps handles every other step. The dashboard makes every step, artifact, configuration, and execution inspectable and re-runnable.

**Lifecycle in one line:**

```
raw video/images → extract frames → auto-label → human review (CVAT) →
commit dataset → export YOLO → dispatch Docker training → model_version
```

The system is **model-agnostic**: it does not care what architecture trains as long as the training container speaks the ICD (interface contract document) for inputs/outputs. It is **format-agnostic** at the annotation layer: geometry is stored canonically; YOLO format is one export projection.

---

## 2. Non-Negotiable Invariants

These two ideas are structural. Every decision in this document derives from them. Violating either breaks reproducibility, deduplication, modularity, or the auto-generated UI.

### Invariant 1: Immutable + Content-Addressed + Append-Only

- Every blob (frame, thumbnail, model weights, export archive, log) is stored **keyed by SHA-256 of its content**: `sha256:<64-hex-chars>`. Identical bytes store exactly once across all projects.
- Dataset commits are **immutable manifests** of `(sample_id, annotation_revision_id, split)` triples. Once inserted, a commit row is never updated or deleted.
- The only mutable state in the system is a small set of **named pointers** (branch heads in `refs`). Every mutation to a branch is a compare-and-swap (CAS) on one row.
- Consequences that fall out for free: deduplication, conflict-free multi-project sharing, safe concurrency with no global locks, perfect reproducibility, and cache correctness (a hash's content is immutable, so `Cache-Control: immutable` is always correct).

### Invariant 2: `type` + JSON Schema + Registry for Every Pluggable Thing

- Every capability that could vary (step type, exporter, storage backend, model runner, labeling backend, split strategy, merge policy, ICD config) registers a `type_key` and a JSON Schema for its configuration.
- Configuration is stored as **JSONB validated against the registered schema at write time**.
- Consequences: adding a new type requires zero changes to core code (only a new plugin file + registration); the schema is the single validation gate; the UI renders config forms directly from schemas — so every new capability is immediately user-drivable.

---

## 3. Nine Cross-Cutting Principles

These are acceptance criteria for every implementation decision.

**P1 — Separate bytes from facts.** PostgreSQL holds only facts (metadata, pointers, counts). Object storage holds only bytes. The API never proxies image data — it issues presigned URLs and the client fetches bytes directly.

**P2 — Immutability by default.** Blobs, commits, annotation revisions, and tags are all immutable. New state is new rows. Old rows are historical facts.

**P3 — Append-only writes; pointers are the only mutable state.** Edits create new rows. The only in-place updates are CAS advances of branch heads and status transitions on `runs` rows.

**P4 — Content-addressing everywhere bytes live.** Every blob is keyed by SHA-256 of its content. Deduplication, immutability, and cache correctness all follow from this single decision.

**P5 — Everything pluggable is `type` + JSON Schema + registry.** No capability is hardcoded into the engine or API. Every pluggable thing registers a `type_key` and schema. Core code resolves by key; it never imports a concrete step class.

**P6 — Nothing in a flow is hidden from the user.** Every step type is listable, every step config is inspectable, every run exposes its exact inputs/outputs/logs/metrics, every gate shows what it waits on. The UI is a window onto the same objects the engine operates on, not a separate reflection.

**P7 — Artifacts in, artifacts out.** Every unit of work consumes versioned artifact references and produces versioned artifact references. Big bytes never pass through the engine in memory — only references (blob hashes, UUIDs).

**P8 — Reproducibility is a first-class output.** For any trained `model_version`, the system must fully answer: exact dataset commit + code version + hyperparams + Docker image + seed. The `trained_on_commit_id` FK is the reproducibility anchor.

**P9 — Least privilege at every boundary.** Clients hold no DB or storage credentials. The API issues narrow, short-lived grants. Workers have scoped roles. Presigned URLs are single-object, short-TTL.

---

## 4. Technology Stack

| Layer | Choice | Notes |
|---|---|---|
| API | FastAPI (Python 3.12), SQLAlchemy 2.x async (asyncpg), Alembic, pydantic-settings | Async throughout |
| Auth | python-jose (HS256 JWT), passlib[bcrypt], OAuth2PasswordBearer | 15-min access / 7-day refresh |
| Frontend | Vite + React 18 + TypeScript (strict), TailwindCSS + shadcn/ui (Radix primitives), TanStack Query v5, Zustand, React Flow (@xyflow/react), react-jsonschema-form (@rjsf/core + @rjsf/tailwind + @rjsf/validator-ajv8), React Router v6, axios | |
| Database | PostgreSQL 16 | |
| Blob storage | MinIO on-prem (Phase 1); S3-compatible — swap to AWS S3 or GCS by changing one config line | Bucket: `cvops-blobs` |
| Job queue | Redis 7 Streams — thin wake-up signal only (`{job_id, step_type, queue}`). Job state lives in PostgreSQL `runs` table, not Redis. | `XADD` / `XREADGROUP` / `XACK` |
| Cache / locks | Redis 7 | Presigned URL cache, distributed locks (branch CAS), SSE pub/sub |
| Workers | Three separate Docker services (Phase 2+); Phase 1 uses synchronous in-process executor via FastAPI `BackgroundTasks`. No Celery. | `worker-preprocessing`, `worker-labeling`, `worker-training` |
| Annotation UI | CVAT self-hosted (Docker Compose service) | Phase 2 |
| Training dispatch | Docker Python SDK (`docker.from_env()`) | Docker socket mounted into `worker-training` only — not the API |
| Deployment | Docker Compose — single `docker compose up` | |
| Blob key scheme | SHA-256, stored as `sha256:<hex>`, MinIO object path: `blobs/{hash[7:9]}/{hash[9:]}` | Two-char prefix sharding |

---

## 5. Complete Data Model

### 5.1 G1 Base Spine (EntityBase Mixin)

Every domain table inherits this mixin via SQLAlchemy `EntityBase`. Omit `project_id` only for `projects` itself and cross-project tables.

```
id              UUID PRIMARY KEY DEFAULT gen_random_uuid()
created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
created_by      UUID REFERENCES users(id)
deleted_at      TIMESTAMPTZ                          -- NULL = live; soft-delete default
project_id      UUID REFERENCES projects(id)         -- omit for orgs/users/projects/blobs/events/type_schemas
```

### 5.2 Infrastructure Tables

#### `blobs`
```sql
hash            TEXT PRIMARY KEY   -- "sha256:<64-hex-chars>"
storage_backend TEXT NOT NULL      -- "minio" | "s3" | "gcs"
storage_key     TEXT NOT NULL      -- path within bucket: blobs/{hash[7:9]}/{hash[9:]}
size_bytes      BIGINT NOT NULL
media_type      TEXT NOT NULL      -- MIME type
created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
```
No FK constraints by design — blobs are referenced from many tables; integrity enforced at app layer.
`INDEX (storage_backend, storage_key)`.

#### `type_schemas`
```sql
type_key        TEXT PRIMARY KEY   -- "step.extract_frames", "exporter.yolo", "icd.training_container"
category        TEXT NOT NULL      -- "step"|"exporter"|"backend"|"model_runner"|"labeling_backend"|"split_strategy"|"merge_policy"|"icd"
json_schema     JSONB NOT NULL     -- JSON Schema for the config object
schema_version  TEXT NOT NULL DEFAULT '1'
ui_hints        JSONB              -- optional: widget overrides, field ordering, visual grouping
created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
```
`INDEX (category)`.

#### `events` (append-only — never UPDATE, never DELETE)
```sql
id              UUID PRIMARY KEY DEFAULT gen_random_uuid()
actor_id        UUID               -- user_id or service identity; NULL for system events
actor_type      TEXT NOT NULL      -- "user" | "service" | "system"
entity_type     TEXT NOT NULL      -- "project"|"sample"|"commit"|"run"|"annotation_revision"|...
entity_id       UUID NOT NULL      -- no FK by design — polymorphic, must survive entity deletion
action          TEXT NOT NULL      -- "created"|"deleted"|"run.started"|"run.succeeded"|"branch.advanced"|...
payload         JSONB              -- action-specific detail
created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
```
`INDEX (entity_type, entity_id, created_at DESC)`.
`INDEX (actor_id, created_at DESC)`.
`INDEX (created_at DESC)` — global activity feed.

### 5.3 Auth Tables

#### `orgs`
```sql
id          UUID PRIMARY KEY DEFAULT gen_random_uuid()
created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
deleted_at  TIMESTAMPTZ
name        TEXT NOT NULL UNIQUE
settings    JSONB NOT NULL DEFAULT '{}'   -- retention_days_soft_delete, retention_days_gc, etc.
```

#### `users`
```sql
id              UUID PRIMARY KEY DEFAULT gen_random_uuid()
created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
deleted_at      TIMESTAMPTZ
org_id          UUID NOT NULL REFERENCES orgs(id)
email           TEXT NOT NULL UNIQUE
password_hash   TEXT                       -- NULL for SSO-only users (Phase 2+)
is_active       BOOLEAN NOT NULL DEFAULT true
```
`INDEX (org_id)`.
`INDEX (email) WHERE deleted_at IS NULL`.

#### `memberships`
```sql
id          UUID PRIMARY KEY DEFAULT gen_random_uuid()
created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
org_id      UUID NOT NULL REFERENCES orgs(id)
user_id     UUID NOT NULL REFERENCES users(id)
role        TEXT NOT NULL  -- "owner" | "maintainer" | "annotator" | "viewer"
UNIQUE (org_id, user_id)
```

#### `refresh_tokens`
```sql
id          UUID PRIMARY KEY DEFAULT gen_random_uuid()
created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
user_id     UUID NOT NULL REFERENCES users(id)
token_hash  TEXT NOT NULL UNIQUE   -- bcrypt hash of the raw token UUID
expires_at  TIMESTAMPTZ NOT NULL
revoked     BOOLEAN NOT NULL DEFAULT false
```
`INDEX (user_id) WHERE NOT revoked`.

### 5.4 Domain Tables

#### `projects`
```sql
-- G1 spine (no project_id column on this table itself)
org_id              UUID NOT NULL REFERENCES orgs(id)
name                TEXT NOT NULL
task_type           TEXT NOT NULL DEFAULT 'detection'  -- "detection"|"segmentation"|"classification"
default_ontology_id UUID REFERENCES ontologies(id)     -- nullable until ontology created
settings            JSONB NOT NULL DEFAULT '{}'
UNIQUE (org_id, name) WHERE deleted_at IS NULL
```
`INDEX (org_id) WHERE deleted_at IS NULL`.

#### `data_sources`
```sql
-- G1 spine + project_id
type            TEXT NOT NULL
-- type is a registered source_type key, e.g.:
--   "source.video"          video file (frames extracted by step.extract_frames)
--   "source.image_folder"   flat folder of images
--   "source.rf_capture"     RF IQ capture file (.bin, .sigmf, etc.)
--   "source.audio"          audio file (.wav, .mp3, etc.)
--   "source.external_uri"   remote URI, no blob upload
-- type is validated against type_schemas at write time (category = 'source_type')
blob_hash       TEXT REFERENCES blobs(hash)         -- for uploaded files; NULL for external_uri
external_uri    TEXT                                -- for remote sources; NULL for uploaded files
status          TEXT NOT NULL DEFAULT 'pending'
-- status ∈ {pending, uploaded, ingesting, ingested, failed}
metadata        JSONB NOT NULL DEFAULT '{}'         -- domain-specific: fps/codec for video, center_freq for RF, etc.
```
`INDEX (project_id) WHERE deleted_at IS NULL`.

#### `data_items`
Replaces the former `samples` table. Domain-agnostic — stores one atomic data unit regardless of type.

```sql
-- G1 spine + project_id
blob_hash       TEXT NOT NULL REFERENCES blobs(hash)
source_id       UUID NOT NULL REFERENCES data_sources(id)
item_type       TEXT NOT NULL
-- item_type matches the source_type that produced it, e.g.:
--   "image"       produced by source.video or source.image_folder
--   "rf_frame"    produced by source.rf_capture
--   "audio_seg"   produced by source.audio
-- validated against type_schemas (category = 'source_type') at write time
metadata        JSONB NOT NULL DEFAULT '{}'
-- domain-specific fields stored here, NOT as columns:
--   image:    {width, height, frame_index, perceptual_hash, thumbnail_hash}
--   rf_frame: {sample_rate, center_freq_hz, bandwidth_hz, duration_ms, capture_offset_ms}
--   audio_seg:{sample_rate, duration_ms, channels, segment_index}
UNIQUE (project_id, blob_hash)    -- one item per unique byte sequence within a project
```
`INDEX (project_id, source_id)`.
`INDEX (project_id, item_type) WHERE deleted_at IS NULL`.

**Thumbnail generation** is domain-driven. Each `item_type` has a registered `thumbnail_generator` in the registry:
- `image` → resize to 256×256 JPEG (as before)
- `rf_frame` → render spectrogram PNG
- `audio_seg` → render waveform PNG
- Default fallback → generic file-type icon blob

Thumbnail blob hash is stored in `metadata.thumbnail_hash` (not a dedicated column).

#### `ontologies`
```sql
-- G1 spine + project_id (nullable — shared ontologies have NULL project_id)
name        TEXT NOT NULL
version     INTEGER NOT NULL DEFAULT 1
UNIQUE (project_id, name) WHERE deleted_at IS NULL
```

#### `label_classes`
```sql
-- G1 spine (no project_id; scoped through ontology_id)
ontology_id     UUID NOT NULL REFERENCES ontologies(id)
class_key       TEXT NOT NULL      -- stable string id, e.g. "vehicle.car"
                                   -- NEVER reuse or reorder existing class_keys
display_name    TEXT NOT NULL
color           TEXT NOT NULL DEFAULT '#FF0000'  -- hex
sort_order      INTEGER NOT NULL   -- determines YOLO class_id at export time
UNIQUE (ontology_id, class_key)
UNIQUE (ontology_id, sort_order)
```
`INDEX (ontology_id)`.

**Critical:** `class_id` in YOLO format is derived at export time from `sort_order`. It is never stored anywhere. Reordering `sort_order` changes exported `class_id` assignments. This is expected and documented — it only affects future exports, not stored annotation data.

#### `annotation_revisions` (append-only — never UPDATE, never DELETE)
```sql
id                  UUID PRIMARY KEY DEFAULT gen_random_uuid()
created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
created_by          UUID NOT NULL REFERENCES users(id)
project_id          UUID NOT NULL REFERENCES projects(id)
data_item_id        UUID NOT NULL REFERENCES data_items(id)   -- renamed from sample_id
ontology_id         UUID NOT NULL REFERENCES ontologies(id)
ontology_version    INTEGER NOT NULL
annotation_type     TEXT NOT NULL
-- annotation_type is a registered key, e.g.:
--   "annotation.cv.detection"     bbox/polygon on an image
--   "annotation.cv.segmentation"  pixel mask on an image
--   "annotation.rf.burst_label"   time+freq region in an RF capture
--   "annotation.audio.segment"    time region in an audio file
-- validated against type_schemas (category = 'annotation_type') at write time
-- payload schema is enforced per annotation_type
revision_no         INTEGER NOT NULL       -- 1-based per data_item; monotonically increasing
parent_revision_id  UUID REFERENCES annotation_revisions(id)  -- NULL for first revision
payload             JSONB NOT NULL         -- shape determined by annotation_type
provenance          JSONB NOT NULL
```

`payload` shape varies by `annotation_type`. Examples:

`annotation.cv.detection`:
```json
[
  {
    "class_key": "vehicle.car",
    "geometry": {
      "type": "bbox",
      "coords": [0.512, 0.331, 0.148, 0.092]
    },
    "attributes": {},
    "confidence": 0.92,
    "track_id": null
  }
]
```

`annotation.rf.burst_label`:
```json
[
  {
    "class_key": "signal.lte_uplink",
    "freq_start_hz": 1920000000,
    "freq_end_hz":   1980000000,
    "time_start_ms": 12.4,
    "time_end_ms":   18.7,
    "confidence": 0.85,
    "attributes": {}
  }
]
```
`geometry.type` ∈ `{bbox, polygon, oriented_bbox, keypoints}`. Coords normalized 0.0–1.0, origin top-left. `bbox` coords are `[cx, cy, w, h]`.

`provenance` shape:
```json
{
  "source": "model",
  "model_version_id": "uuid-or-null",
  "author_user_id": "uuid-or-null",
  "confidence_threshold": 0.35,
  "review_status": "unreviewed"
}
```
`source` ∈ `{model, human, import, merge}`.
`review_status` ∈ `{unreviewed, accepted, rejected, needs_second_review}`.

`INDEX (sample_id, revision_no)`.
`INDEX (project_id, created_at DESC)`.
No soft-delete column — revisions are historical facts; they are superseded by newer revisions but never deleted.

#### `datasets`
```sql
-- G1 spine + project_id
name        TEXT NOT NULL
UNIQUE (project_id, name) WHERE deleted_at IS NULL
```

#### `commits` (immutable — never UPDATE)
```sql
id                      UUID PRIMARY KEY DEFAULT gen_random_uuid()
created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
created_by              UUID NOT NULL REFERENCES users(id)
project_id              UUID NOT NULL REFERENCES projects(id)
dataset_id              UUID NOT NULL REFERENCES datasets(id)
parent_commit_id        UUID REFERENCES commits(id)           -- NULL for root commit
second_parent_commit_id UUID REFERENCES commits(id)           -- non-NULL for merge commits only
ontology_id             UUID NOT NULL REFERENCES ontologies(id)
ontology_version        INTEGER NOT NULL
message                 TEXT NOT NULL DEFAULT ''
stats                   JSONB NOT NULL DEFAULT '{}'
```

`stats` shape:
```json
{
  "total": 4200,
  "by_split": {"train": 3360, "val": 840, "test": 0},
  "by_class": {"vehicle.car": 1800, "vehicle.truck": 620},
  "review_status": {"accepted": 3100, "unreviewed": 1100}
}
```
Stats are computed once at commit time and cached forever — the commit is immutable so stats never go stale.

`INDEX (dataset_id, created_at DESC)`.
`INDEX (parent_commit_id)`.

#### `commit_samples`
```sql
commit_id               UUID NOT NULL REFERENCES commits(id)
sample_id               UUID NOT NULL REFERENCES samples(id)
annotation_revision_id  UUID NOT NULL REFERENCES annotation_revisions(id)
split                   TEXT NOT NULL  -- "train" | "val" | "test"
PRIMARY KEY (commit_id, sample_id)
```
`INDEX (commit_id, split)`.
`INDEX (sample_id)`.

#### `refs`
```sql
id                  UUID PRIMARY KEY DEFAULT gen_random_uuid()
created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
created_by          UUID NOT NULL REFERENCES users(id)
dataset_id          UUID NOT NULL REFERENCES datasets(id)
ref_type            TEXT NOT NULL   -- "branch" | "tag"
name                TEXT NOT NULL   -- "main", "experiment-v2", "v1.0"
target_commit_id    UUID NOT NULL REFERENCES commits(id)
is_mutable          BOOLEAN NOT NULL  -- true for branches, false for tags
UNIQUE (dataset_id, ref_type, name)
```
No soft-delete — deletion is hard (audited via `events`).
`INDEX (dataset_id)`.

#### `project_dataset_links`
```sql
-- G1 spine + project_id
dataset_id          UUID NOT NULL REFERENCES datasets(id)
pinned_commit_id    UUID REFERENCES commits(id)   -- exactly one of these two must be non-null
ref_id              UUID REFERENCES refs(id)
CHECK (
  (pinned_commit_id IS NOT NULL AND ref_id IS NULL) OR
  (pinned_commit_id IS NULL     AND ref_id IS NOT NULL)
)
UNIQUE (project_id, dataset_id)
```

#### `model_versions`
```sql
-- G1 spine + project_id
blob_hash               TEXT NOT NULL REFERENCES blobs(hash)  -- model weights (tar.gz)
trained_on_commit_id    UUID NOT NULL REFERENCES commits(id)
training_container_id   UUID NOT NULL REFERENCES training_containers(id)
base_model              TEXT            -- "yolov8n", "yolov8s", etc. (free text)
hyperparams             JSONB NOT NULL DEFAULT '{}'
metrics                 JSONB NOT NULL DEFAULT '{}'  -- {"mAP50": 0.87, "mAP50-95": 0.62, ...}
code_version            TEXT            -- git SHA of training code repo (optional)
env_hash                TEXT            -- hash of Dockerfile or requirements (optional)
seed                    INTEGER
mlflow_run_id           TEXT            -- Phase 2+, nullable
```
`INDEX (project_id, created_at DESC)`.
`INDEX (trained_on_commit_id)`.

#### `training_containers`
```sql
-- G1 spine + project_id
name                TEXT NOT NULL
description         TEXT
image               TEXT NOT NULL        -- Docker image tag, e.g. "my-org/yolo-trainer:latest"
icd_config          JSONB NOT NULL
icd_schema_version  TEXT NOT NULL DEFAULT '1.0'
UNIQUE (project_id, name) WHERE deleted_at IS NULL
```

`icd_config` shape:
```json
{
  "inputs": {
    "dataset_path": {"env": "DATASET_PATH"},
    "epochs":        {"env": "EPOCHS"},
    "batch_size":    {"env": "BATCH_SIZE"},
    "seed":          {"env": "SEED"}
  },
  "outputs": {
    "metrics_file":  {"path": "/output/metrics.json"},
    "weights_path":  {"path": "/output/weights/"}
  },
  "volume_mount":    "/data/dataset",
  "mlflow_tracking_uri": null
}
```
Validated against `type_schemas` row for `icd.training_container` at every write.

#### `workflows`
```sql
-- G1 spine + project_id
name            TEXT NOT NULL
definition      JSONB NOT NULL     -- {steps: [...], edges: [[from_id, to_id], ...]}
version         INTEGER NOT NULL DEFAULT 1
UNIQUE (project_id, name) WHERE deleted_at IS NULL
```

Step definition shape within `definition.steps`:
```json
{
  "id": "s1",
  "type": "step.extract_frames",
  "config": {"interval_seconds": 2},
  "inputs": {"source": "$run.params.source_id"}
}
```

Reference language for `inputs`:
- `$run.params.<name>` — workflow-level parameter passed at run time
- `$steps.<id>.outputs.<name>` — output of a preceding step

#### `runs`
```sql
id                  UUID PRIMARY KEY DEFAULT gen_random_uuid()
created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
created_by          UUID NOT NULL REFERENCES users(id)
project_id          UUID NOT NULL REFERENCES projects(id)
kind                TEXT NOT NULL      -- "workflow" | "step" | "gc"
parent_run_id       UUID REFERENCES runs(id)
-- NULL for workflow runs; step runs reference their parent workflow run
workflow_id         UUID REFERENCES workflows(id)
workflow_version    INTEGER
step_id             TEXT               -- the step's "id" field in the workflow definition
step_type           TEXT               -- "step.extract_frames" etc.
status              TEXT NOT NULL DEFAULT 'pending'
-- status ∈ {pending, running, waiting, succeeded, failed, canceled}
input_refs          JSONB NOT NULL DEFAULT '{}'
output_refs         JSONB NOT NULL DEFAULT '{}'
config              JSONB NOT NULL DEFAULT '{}'   -- validated snapshot of config at run time
metrics             JSONB NOT NULL DEFAULT '{}'
logs_blob_hash      TEXT REFERENCES blobs(hash)
attempt             INTEGER NOT NULL DEFAULT 1
error               TEXT
started_at          TIMESTAMPTZ
finished_at         TIMESTAMPTZ
```
`INDEX (workflow_id, status)`.
`INDEX (parent_run_id)`.
`INDEX (project_id, created_at DESC)`.
`INDEX (status) WHERE status IN ('pending', 'running', 'waiting')`.

#### `labeling_jobs`
```sql
-- G1 spine + project_id
run_id              UUID NOT NULL REFERENCES runs(id)
step_id             TEXT NOT NULL
cvat_project_id     INTEGER
cvat_task_id        INTEGER NOT NULL
cvat_job_ids        JSONB NOT NULL DEFAULT '[]'   -- array of CVAT integer job IDs
status              TEXT NOT NULL DEFAULT 'pushed'
-- status ∈ {pushed, in_progress, completed, failed}
completed_at        TIMESTAMPTZ
sync_error          TEXT
sample_count        INTEGER NOT NULL
annotation_revision_ids_in   JSONB   -- revision IDs sent as pre-labels
annotation_revision_ids_out  JSONB   -- revision IDs created after pull-back
```
`INDEX (run_id)`.
`INDEX (cvat_task_id)`.
`INDEX (project_id, status) WHERE deleted_at IS NULL`.

### 5.5 Alembic Migration Files (ordered)

```
001_initial.py              -- confirms plumbing; empty schema
002_auth_tables.py          -- orgs, users, memberships, refresh_tokens
003_blobs_events_schemas.py -- blobs, events, type_schemas
004_projects_data_sources.py
005_samples_ontologies.py   -- samples, ontologies, label_classes
006_annotation_revisions.py
007_versioning.py           -- datasets, commits, commit_samples, refs, project_dataset_links
008_workflows_runs.py
009_models_containers.py    -- model_versions, training_containers
010_labeling_jobs.py
```

---

## 6. Versioning, Concurrency, and Merge

### 6.1 Core Objects

| Object | Mutable? | Role |
|---|---|---|
| Commit | No | Frozen snapshot: `(sample → annotation_revision, split)` manifest + ontology version + parent(s) |
| Branch | Yes (a pointer) | Moving head pointing at the latest commit on a line of work |
| Tag | No (a fixed pointer) | Permanent name for one commit; what training pins to |

### 6.2 The Save Lifecycle

Every mutation is append rows + move one pointer.

1. **Save bytes:** Hash → dedup-check `blobs` → if absent, upload to MinIO → insert `blobs` row. Idempotent.
2. **Save annotation edit:** Insert new `annotation_revision` with `parent_revision_id` set, `revision_no` incremented. Old revision untouched.
3. **Commit dataset state:** Insert `commits` row with `parent_commit_id = current branch head`. Batch-insert `commit_samples`. CAS-advance the branch (see §6.3).
4. **Tag a version:** Insert immutable `refs` row (`is_mutable = false`, `ref_type = 'tag'`).

### 6.3 CAS Branch Advance

```sql
UPDATE refs
SET target_commit_id = :new_commit_id, updated_at = now()
WHERE id = :ref_id
  AND target_commit_id = :expected_head
```

If 0 rows updated: reload the head, check if new samples actually conflict (same sample UUID, different revision) — if no real conflict, update the new commit's `parent_commit_id` to the current head and retry. Maximum 3 retries. On 3rd failure raise a conflict error with the differing sample IDs.

This is optimistic concurrency. No dataset-level lock. Readers and parallel-branch writers are completely unaffected.

### 6.4 Merging Two Branches

A merge is a set union of `commit_sample` rows. For each sample:

| Situation | Action |
|---|---|
| Sample on one side only | Include it (no conflict) |
| Same sample, same `annotation_revision_id` | Include once (no conflict) |
| Same sample, different revisions | Apply merge policy |

Default merge policy: `merge_policy.human_over_model`. Resolution order: human-reviewed beats human-unreviewed beats model. If both sides are human-reviewed with different content → escalate to `manual`.

A merge commit has both `parent_commit_id` and `second_parent_commit_id` set.

### 6.5 Multi-Project Sharing Without Conflict

A project does not own a dataset — it holds a `project_dataset_link` row referencing either:
- `pinned_commit_id` — immutable snapshot; reproducible; never changes under the project
- `ref_id` — floating on a branch; sees new commits as the branch advances

Two projects pinned to the same commit cannot conflict because the commit is read-only. Projects on different branches are fully independent. The only shared objects are immutable commits.

### 6.6 Garbage Collection

- **Reachability:** A commit is reachable if any `ref` row or `project_dataset_link` can reach it through the parent graph.
- **Soft-delete window:** 30 days default (configurable in `orgs.settings`). Hard deletion only after window expires.
- **Pinned commits:** A commit referenced by any `project_dataset_link.pinned_commit_id` is never GC-eligible, even if its branch is deleted.
- **Blob reference counting:** A blob is deletable only when no reachable `sample`, `model_version`, `labeling_job`, or `run` references it. Weekly GC sweep; minimum blob age 30 days before hard-delete.
- **GC runs** are `runs` rows with `kind = 'gc'`, fully audited via `events`.

---

## 7. Storage and Blob Access

### 7.1 Two Stores, Opposite Responsibilities

PostgreSQL holds only facts: metadata, pointers, counts. Object storage holds only bytes, keyed by content hash. The link is a `blobs` row mapping `hash → (storage_backend, storage_key)`.

Assembling a filtered dataset version — *"all samples in commit X, split=train, class=car, review_status=accepted"* — is a SQL query over tiny rows in `commit_samples` and `annotation_revisions`. Zero image bytes are read to answer that query.

### 7.2 Presigned URL Pattern

The API never proxies image bytes. Authorization is enforced at the API; bytes flow directly from MinIO to the client.

**Read flow:**
1. `GET /samples/{id}/image-url` — API authorizes the actor against the project, looks up `blob_hash → storage_key`, signs the URL.
2. API returns `{"url": "<presigned GET, 15-min TTL>"}`.
3. Client fetches bytes directly from MinIO.

**Upload flow:**
1. `POST /projects/{id}/data-sources` — API creates a `data_sources` row, calls `storage.get_presigned_put()`, returns the presigned PUT URL.
2. Client PUTs directly to MinIO.
3. Client calls `POST /data-sources/{id}/confirm-upload` with the `blob_hash` it computed. The API promotes the upload to its content-addressed key (server-side copy), inserts the `blobs` row, sets the data source to `uploaded`, and (if the project has `default_ingest_workflow_id`) auto-dispatches that ingest workflow. The SHA-256 is verified lazily when `extract_frames` reads the bytes, not at confirm time.

### 7.3 MinIO Configuration

- Bucket: `cvops-blobs`
- Object path: `blobs/{hash[7:9]}/{hash[9:]}` — two-char prefix derived from positions 7–8 of the hex (not positions 0–1, which have low entropy for SHA-256).
- On API startup: create bucket if not exists; set CORS to allow presigned URL access from the frontend origin.
- `boto3` with `endpoint_url = settings.MINIO_ENDPOINT`.

### 7.4 Caching Rules

Content-addressed objects have permanently stable content. Cache accordingly:

| Asset | Cache policy |
|---|---|
| Frame image (full-res) | `Cache-Control: immutable, max-age=31536000` |
| Thumbnail | `Cache-Control: immutable, max-age=31536000` |
| Export archive | `Cache-Control: immutable, max-age=31536000` |
| Commit row (`GET /datasets/{id}/commits/{cid}`) | `Cache-Control: immutable` |
| Run status | No cache (mutable) |
| Presigned URL itself | Browser-local only, for URL lifetime |

### 7.5 Thumbnails

Every sample gets a thumbnail on ingest. Algorithm: `image.thumbnail((256, 256), PIL.Image.LANCZOS)`, save as JPEG, upload via `storage.save_bytes()`, store `thumbnail_hash` on the `samples` row. The thumbnail grid in the UI never loads full-resolution images.

---

## 8. Registry — All Registered Types

All types are registered at API startup by calling `registry.register()` and are synced to the `type_schemas` table. The `GET /registry/types` endpoint exposes them to the UI.

### ui_hints and ui_group

Every registered type carries a `ui_hints` JSONB column in `type_schemas`. The `ui_group` field inside `ui_hints` controls how the workflow builder palette groups steps into sections. The frontend reads `ui_hints.group` and renders grouped, labelled sections — no hardcoded lists anywhere.

```json
// Example ui_hints on step.extract_frames:
{
  "group":       "Data Preprocessing",
  "icon":        "video",
  "description": "Extract frames from a video source at a configured interval",
  "order":       1
}
```

`GET /registry/types?category=step` returns all registered steps including `ui_hints`. The workflow builder palette is built entirely from this response.

**Palette groups (current):**

| group | Steps shown |
|---|---|
| `Data Preprocessing` | extract_frames, auto_label, denoise_rf (future), commit_dataset, export_yolo |
| `Labeling` | human_review |
| `Training` | train, evaluate |

Adding a new step with a new `group` value creates a new palette section automatically.

### 8.1 Step Registry (`category = 'step'`)

Steps are routed to workers by a `queue` field on each step class. Workers only pick up jobs for their queue.

| type_key | queue | ui_group | config fields | inputs | outputs | Gate? |
|---|---|---|---|---|---|---|
| `step.extract_frames` | `preprocessing` | Data Preprocessing | `interval_seconds: float`, `max_frames: int?`, `dedup_threshold: float?` | `source_id: str` | `data_item_ids: str[]` | No |
| `step.auto_label` | `preprocessing` | Data Preprocessing | `model_version_id: str`, `confidence_threshold: float` | `data_item_ids: str[]` | `annotation_revision_ids: str[]` | No |
| `step.commit_dataset` | `preprocessing` | Data Preprocessing | `dataset_name: str`, `branch_name: str`, `split_strategy: str`, `train_ratio: float`, `val_ratio: float`, `ontology_id: str` | `data_item_ids: str[]`, `annotation_revision_ids: str[]` | `commit_id: str`, `ref_id: str` | No |
| `step.export_yolo` | `preprocessing` | Data Preprocessing | `ontology_id: str?` | `commit_id: str` | `export_blob_hash: str` | No |
| `step.human_review` | `labeling` | Labeling | `labeling_backend: str` (default "cvat"), `assignees: str[]?` | `annotation_revision_ids: str[]` | `annotation_revision_ids: str[]` | **Yes** |
| `step.train` | `training` | Training | `training_container_id: str`, `hyperparams: object` | `export_blob_hash: str` | `model_version_id: str` | No |
| `step.evaluate` | `training` | Training | `eval_commit_id: str` | `model_version_id: str` | `metrics: object` | No (Phase 3) |

### 8.2 Exporter Registry (`category = 'exporter'`)

| type_key | Phase |
|---|---|
| `exporter.yolo` | 1 |
| `exporter.coco` | 3 |
| `exporter.voc` | 3 |

### 8.3 Storage Backend Registry (`category = 'backend'`)

| type_key | Notes |
|---|---|
| `backend.minio` | Phase 1 default |
| `backend.s3` | Phase 1 (config-swap) |
| `backend.gcs` | Phase 1 (config-swap) |

### 8.4 Split Strategy Registry (`category = 'split_strategy'`)

| type_key | Behavior |
|---|---|
| `split_strategy.by_source_group` | Groups data_items by `source_id`; assigns the entire group to one split using `sha256(source_id + dataset_id) % 100`. Enforced default for video/capture-derived data. Prevents split leakage. |
| `split_strategy.random_seeded` | Per-item random assignment. Requires explicit opt-in with a UI warning about split leakage risk. |

### 8.5 Merge Policy Registry (`category = 'merge_policy'`)

| type_key | Behavior |
|---|---|
| `merge_policy.human_over_model` | human-reviewed > human-unreviewed > model-labeled. Conflict between two human-reviewed different revisions → escalate to `manual`. Default. |
| `merge_policy.newest` | Higher `revision_no` wins. |
| `merge_policy.manual` | All conflicts queued for user resolution in UI. |

### 8.6 ICD Registry (`category = 'icd'`)

| type_key | Purpose |
|---|---|
| `icd.training_container` | JSON Schema for `training_containers.icd_config`. Validated at every write to `training_containers`. |

### 8.7 Annotation Type Registry (`category = 'annotation_type'`)

Each `annotation_type` registers a JSON Schema. `annotation_revisions.payload` is validated against it at write time.

| type_key | Domain | Payload shape |
|---|---|---|
| `annotation.cv.detection` | Computer Vision | `[{class_key, geometry: {type, coords}, confidence, attributes, track_id}]` |
| `annotation.cv.segmentation` | Computer Vision | `[{class_key, mask_blob_hash, confidence, attributes}]` |
| `annotation.rf.burst_label` | RF / SDR | `[{class_key, freq_start_hz, freq_end_hz, time_start_ms, time_end_ms, confidence}]` |
| `annotation.audio.segment` | Audio | `[{class_key, time_start_ms, time_end_ms, confidence, attributes}]` |

New domains add a new row here. Core code never changes.

### 8.8 Source Type Registry (`category = 'source_type'`)

Defines valid `data_sources.type` values and what `item_type` each source produces.

| type_key | Produces item_type | Notes |
|---|---|---|
| `source.video` | `image` | frames extracted by `step.extract_frames` |
| `source.image_folder` | `image` | images ingested directly |
| `source.rf_capture` | `rf_frame` | IQ capture file (.bin, .sigmf) |
| `source.audio` | `audio_seg` | audio file (.wav, .mp3) |
| `source.external_uri` | any | remote URI, no blob upload |

### 8.9 Thumbnail Generator Registry (`category = 'thumbnail_generator'`)

One generator registered per `item_type`. Called at ingest time. Output blob hash stored in `data_items.metadata.thumbnail_hash`.

| type_key | item_type | Output |
|---|---|---|
| `thumbnail.image` | `image` | 256×256 JPEG (LANCZOS resize) |
| `thumbnail.rf_frame` | `rf_frame` | spectrogram PNG |
| `thumbnail.audio_seg` | `audio_seg` | waveform PNG |

### 8.10 JSON Schema Files (location in repo)

```
packages/steps/src/cvops_steps/schemas/
  extract_frames.json
  auto_label.json
  commit_dataset.json
  export_yolo.json
  train.json

packages/api/src/cvops_api/schemas/annotation_types/
  cv_detection.json
  cv_segmentation.json
  rf_burst_label.json
  audio_segment.json
```

These are loaded and registered at startup. They are also the source of truth for the UI config forms rendered via `react-jsonschema-form`.

---

## 9. Step Contract and Executor

### 9.1 Step Base Class

Location: `services/api/src/cvops_api/engine/step.py`

```python
from dataclasses import dataclass
from typing import Any
from sqlalchemy.ext.asyncio import AsyncSession
from cvops_api.core.storage import StorageBackend

@dataclass
class StepContext:
    session: AsyncSession
    storage: StorageBackend
    project_id: str
    run_id: str          # UUID of the step run row
    actor_id: str        # user who triggered the workflow run
    audit: Any           # bound emit_event coroutine

class GateException(Exception):
    """Raised by gate steps to pause the run. gate_data stored in run.output_refs."""
    def __init__(self, gate_data: dict):
        self.gate_data = gate_data

class Step:
    type_key:     str  = ""     # e.g. "step.extract_frames"
    queue:        str  = ""     # "preprocessing" | "labeling" | "training"
    config_schema: dict = {}    # JSON Schema — validated before run; also drives UI form
    is_gate:      bool = False
    ui_hints:     dict = {}
    # ui_hints shape:
    # {
    #   "group":       "Data Preprocessing",  <- palette section in workflow builder
    #   "icon":        "video",               <- icon name (from icon library)
    #   "description": "Human-readable text shown in palette",
    #   "order":       1                      <- sort order within the group
    # }

    async def run(self, ctx: StepContext, config: dict, inputs: dict) -> dict:
        raise NotImplementedError

    def idempotency_key(self, config: dict, inputs: dict) -> str:
        import hashlib, json
        return hashlib.sha256(
            json.dumps(
                {"type": self.type_key, "config": config, "inputs": inputs},
                sort_keys=True
            ).encode()
        ).hexdigest()
```

**Registration** (called at module import in `packages/steps/src/cvops_steps/__init__.py`):

```python
from cvops_api.core.registry import registry
from cvops_steps.extract_frames import ExtractFramesStep
from cvops_steps.auto_label import AutoLabelStep
from cvops_steps.human_review import HumanReviewStep
from cvops_steps.commit_dataset import CommitDatasetStep
from cvops_steps.export_yolo import ExportYoloStep
from cvops_steps.train import TrainStep

def register_all():
    registry.register(ExtractFramesStep())
    registry.register(AutoLabelStep())
    registry.register(HumanReviewStep())
    registry.register(CommitDatasetStep())
    registry.register(ExportYoloStep())
    registry.register(TrainStep())
```

### 9.2 Registry Implementation

Location: `services/api/src/cvops_api/core/registry.py`

- In-memory `dict[str, Step]` singleton.
- On `register(step)`: insert/upsert a row into `type_schemas` with `type_key = step.type_key`, `category = 'step'`, `json_schema = step.config_schema`. Sync happens at startup via the entrypoint script.
- `resolve(type_key: str) -> Step` — look up by key; raise `KeyError` if not found.
- `validate_config(type_key: str, config: dict) -> None` — validate `config` against the registered JSON Schema using `jsonschema.validate()`; raise `ValidationError` on failure.
- `list_by_category(category: str) -> list[dict]` — returns `{type_key, json_schema, ui_hints}` dicts from `type_schemas` table.

### 9.3 Synchronous Executor (Phase 1)

Location: `services/api/src/cvops_api/engine/executor.py`

Owner: Itai

```python
async def execute_workflow(
    session: AsyncSession,
    storage: StorageBackend,
    workflow_run_id: str,
) -> None:
    """
    1. Load workflow run row; load workflow definition.
    2. Topological sort of steps using definition.edges (Kahn's algorithm).
    3. For each step in sorted order:
       a. If a step run with this step_id and parent_run_id already exists
          with status='succeeded': collect its output_refs and continue (resume semantics).
       b. Create step run row: kind='step', status='running', parent_run_id=workflow_run_id,
          step_id, step_type, config (validated snapshot), input_refs (resolved).
       c. Validate config against registry schema.
       d. Resolve input_refs: evaluate $steps.<id>.outputs.<name> references
          against completed step run output_refs (via ref_resolver.py).
       e. Compute idempotency key. If a prior step run with same key and status='succeeded'
          exists (for any run in this project): copy its output_refs and mark this step
          run succeeded immediately.
       f. Call step.run(ctx, config, resolved_inputs).
       g. On return: write output_refs, set status='succeeded', emit events row.
       h. On GateException: set step run status='waiting', set workflow run
          status='waiting', store gate_data in output_refs, stop execution.
       i. On any other Exception: set step run status='failed', write error,
          emit events row, set workflow run status='failed', stop execution.
    4. If all steps succeed: set workflow run status='succeeded', emit events row.
    """

async def resume_workflow(
    session: AsyncSession,
    storage: StorageBackend,
    workflow_run_id: str,
    resumed_outputs: dict,
) -> None:
    """
    Called after a gate is satisfied (CVAT webhook / manual resolution).
    1. Find the step run with status='waiting' and parent_run_id=workflow_run_id.
    2. Write resumed_outputs to its output_refs.
    3. Set its status='succeeded'.
    4. Call execute_workflow() to continue from the next step.
    """
```

**FastAPI integration:** `POST /workflows/{id}/runs` calls `execute_workflow()` via `BackgroundTasks.add_task()`. The HTTP response returns the created run row immediately; execution proceeds in the background.

### 9.4 Reference Resolver

Location: `services/api/src/cvops_api/engine/ref_resolver.py`

Evaluates reference strings like `$steps.s2.outputs.annotation_revision_ids` against a dict of completed step run `output_refs`. Raises `ResolutionError` if a referenced step ID does not exist or its output key is absent.

Also evaluates `$run.params.<name>` against the workflow run's `input_refs.params` dict.

---

## 10. Step Implementations

### 10.1 `step.extract_frames` (Nati/Yahav)

Location: `packages/steps/src/cvops_steps/extract_frames.py`

**Input:** `{"source_id": "<uuid>"}`
**Config:** `{"interval_seconds": 2.0, "max_frames": 5000, "dedup_threshold": 0.05}`
**Output:** `{"sample_ids": ["<uuid>", ...]}`

**Algorithm:**
1. Load `data_sources` row; download blob by `blob_hash` to a temp file via `ctx.storage.get_bytes()`.
2. Run FFmpeg:
   ```
   ffmpeg -i {input} -vf fps=1/{interval_seconds} -vsync 0 -frame_pts 1 {output_dir}/%06d.jpg
   ```
   Use `ffmpeg-python` or `subprocess`. The flags `-vsync 0 -frame_pts 1` ensure deterministic, reproducible frame indices. Log the FFmpeg version string to provenance in the run's `config` snapshot.
3. For each extracted JPEG:
   - Compute SHA-256 of the JPEG bytes.
   - Check `samples` table: if `(project_id, blob_hash)` already exists → skip (exact dedup).
   - Compute `imagehash.phash(PIL.Image.open(path))`.
   - Check `samples` table: if any sample in the project has `perceptual_hash` within Hamming distance `dedup_threshold * 64` → skip (near-dedup).
   - Generate thumbnail: `image.thumbnail((256, 256), PIL.Image.LANCZOS)`, save as JPEG.
   - Upload frame JPEG via `ctx.storage.save_bytes(data, "image/jpeg")` → `blob_hash`.
   - Upload thumbnail JPEG via `ctx.storage.save_bytes(data, "image/jpeg")` → `thumbnail_hash`.
   - Insert `samples` row: `blob_hash`, `thumbnail_hash`, `source_id`, `frame_index`, `perceptual_hash`, `width`, `height`, `project_id`.
4. Update `data_sources.status = 'ingested'`.
5. Return `{"sample_ids": [...]}`.

**Deduplication threshold:** Hamming distance threshold = `dedup_threshold * 64`. Default `dedup_threshold = 0.05` means frames must differ by more than 3.2 bits of phash to be kept. Configurable in the step config so operators can tune per-project.

### 10.2 `step.auto_label` (Nati/Yahav)

Location: `packages/steps/src/cvops_steps/auto_label.py`

**Input:** `{"sample_ids": ["<uuid>", ...]}`
**Config:** `{"model_version_id": "<uuid>", "confidence_threshold": 0.35}`
**Output:** `{"annotation_revision_ids": ["<uuid>", ...]}`

**Algorithm:**
1. Load `model_versions` row by `model_version_id`; download weights blob to a temp file.
2. Load model: `model = YOLO(weights_path)` (Ultralytics).
3. Load the model's ontology: query `model_versions.trained_on_commit_id` → `commits.ontology_id` → `label_classes` ordered by `sort_order`. Build a list `[class_key_0, class_key_1, ...]` indexed by YOLO integer class index.
4. For each sample:
   - Download frame bytes from MinIO: `frame_bytes = await ctx.storage.get_bytes(sample.blob_hash)`.
   - Run inference: `results = model(frame_path, conf=confidence_threshold)`.
   - For each box in `results[0].boxes`:
     - `cls_id = int(box.cls.item())`
     - `class_key = class_key_list[cls_id]`
     - `cx, cy, w, h = box.xywhn[0].tolist()` (already normalized 0–1)
     - Build annotation object: `{"class_key": class_key, "geometry": {"type": "bbox", "coords": [cx, cy, w, h]}, "confidence": float(box.conf.item()), "attributes": {}, "track_id": null}`
   - Determine `revision_no`: `MAX(revision_no) + 1` for this `sample_id` (or 1 if no prior revisions).
   - Insert `annotation_revisions` row: `payload = [<annotation objects>]`, `provenance = {"source": "model", "model_version_id": "<id>", "confidence_threshold": 0.35, "review_status": "unreviewed"}`.
5. Return `{"annotation_revision_ids": [...]}`.

### 10.3 `step.human_review` (Yehuda — Phase 2)

Location: `packages/steps/src/cvops_steps/human_review.py`

**Input:** `{"annotation_revision_ids": ["<uuid>", ...]}`
**Config:** `{"labeling_backend": "cvat", "assignees": []}`
**Output (after gate resolves):** `{"annotation_revision_ids": ["<uuid>", ...]}` (reviewed revisions)
**`is_gate = True`**

Raises `GateException({"labeling_job_id": "<uuid>"})`. Full flow documented in §11.

### 10.4 `step.commit_dataset` (Yehuda)

Location: `packages/steps/src/cvops_steps/commit_dataset.py`

**Input:** `{"sample_ids": [...], "annotation_revision_ids": [...]}`
**Config:** `{"dataset_name": "traffic-cams", "branch_name": "main", "split_strategy": "by_source_group", "train_ratio": 0.8, "val_ratio": 0.2, "ontology_id": "<uuid>"}`
**Output:** `{"commit_id": "<uuid>", "ref_id": "<uuid>"}`

**Algorithm:**
1. Resolve or create `datasets` row by `(project_id, dataset_name)`.
2. Resolve or create `refs` row for branch `(dataset_id, 'branch', branch_name)`.
3. Load ontology from `ontology_id`.
4. Assign splits using `split_strategy`:
   - `by_source_group`: Load each sample's `source_id`. Group sample IDs by `source_id`. For each source group, compute `group_hash = sha256((source_id + dataset_id).encode()).hexdigest()`. Use `int(group_hash, 16) % 100` to assign: value `< train_ratio * 100` → `"train"`, `< (train_ratio + val_ratio) * 100` → `"val"`, else → `"test"`. Store `{sample_id: split}` mapping.
   - `random_seeded`: Use `random.seed(sha256(dataset_id + branch_name))`, shuffle sample IDs, cut at `train_ratio`/`val_ratio` boundaries.
5. Map `annotation_revision_ids` to `sample_id`. If multiple revisions passed for the same sample, use the one with the highest `revision_no`.
6. Compute `stats` JSONB: count by split, count by class_key (iterate payloads), count by review_status.
7. Load current branch head: `current_head = refs.target_commit_id`.
8. Insert `commits` row: `parent_commit_id = current_head`, `ontology_id`, `ontology_version`, `stats`.
9. Batch-insert `commit_samples` rows.
10. CAS branch advance:
    ```sql
    UPDATE refs SET target_commit_id = :new_commit_id, updated_at = now()
    WHERE id = :ref_id AND target_commit_id = :expected_head
    ```
    If 0 rows updated: reload head, compare sample sets. If no conflict (no overlapping sample UUIDs with different revisions), set `commits.parent_commit_id = new_head` and retry CAS. Up to 3 retries. On 3rd failure raise `CommitConflictError` with conflicting sample IDs.
11. Emit `events` row: `action = "branch.advanced"`, payload `{old_head, new_head, commit_id}`.
12. Return `{"commit_id": "...", "ref_id": "..."}`.

### 10.5 `step.export_yolo` (Nati/Yahav)

Location: `packages/steps/src/cvops_steps/export_yolo.py`

**Input:** `{"commit_id": "<uuid>"}`
**Config:** `{"ontology_id": "<uuid>?"}` (defaults to the commit's `ontology_id`)
**Output:** `{"export_blob_hash": "sha256:..."}`

**Idempotency key:** includes `(commit_id, effective_ontology_id)`. If a prior step run with the same key and `status = 'succeeded'` exists in this project, return its `output_refs.export_blob_hash` immediately.

**Algorithm:**
1. Load commit, all `commit_samples` rows (with joined `annotation_revisions`), and `label_classes` for the ontology ordered by `sort_order`.
2. Build class list: `names = [lc.class_key for lc in label_classes_ordered_by_sort_order]`. Index in this list is the YOLO `class_id`.
3. Create temp directory structure:
   ```
   {tmpdir}/
     data.yaml
     images/train/  images/val/  images/test/
     labels/train/  labels/val/  labels/test/
   ```
4. Write `data.yaml`:
   ```yaml
   nc: <N>
   names: [<class_key_0>, <class_key_1>, ...]
   path: /data/dataset
   train: images/train
   val: images/val
   test: images/test
   ```
5. For each `commit_sample` row:
   - Download frame bytes from MinIO; write to `images/{split}/{sample_id}.jpg`.
   - For each annotation object in the revision `payload`:
     - `class_index = names.index(annotation["class_key"])`
     - Geometry: `{"type": "bbox", "coords": [cx, cy, w, h]}` → already normalized
     - Write line: `{class_index} {cx} {cy} {w} {h}\n` to `labels/{split}/{sample_id}.txt`
6. Create `tar.gz` archive of the whole temp directory.
7. Upload archive via `ctx.storage.save_bytes(archive_bytes, "application/x-tar")` → `export_blob_hash`.
8. Return `{"export_blob_hash": "..."}`.

### 10.6 `step.train` (Nati/Yahav)

Location: `packages/steps/src/cvops_steps/train.py`

**Input:** `{"export_blob_hash": "sha256:..."}`
**Config:** `{"training_container_id": "<uuid>", "hyperparams": {"epochs": 100, "batch_size": 16, "seed": 42}}`
**Output:** `{"model_version_id": "<uuid>"}`

**Algorithm:**
1. Load `training_containers` row by `training_container_id`.
2. Download export archive from MinIO; extract to `{tmpdir}/dataset/`.
3. Create `{tmpdir}/output/`.
4. Build environment dict from `icd_config.inputs`:
   - For each key in `inputs`: map the key name to its env var name.
   - Set `DATASET_PATH = {tmpdir}/dataset` (always).
   - For each hyperparam key: `env_name = inputs[key]["env"]`; `env_dict[env_name] = str(hyperparams[key])`.
5. Build volumes: `{tmpdir}/dataset → icd_config.volume_mount (ro)`, `{tmpdir}/output → /output (rw)`.
6. Run container synchronously (Phase 1):
   ```python
   import docker
   client = docker.from_env()
   logs = client.containers.run(
       image=training_container.image,
       environment=env_dict,
       volumes={
           f"{tmpdir}/dataset": {"bind": icd_config["volume_mount"], "mode": "ro"},
           f"{tmpdir}/output": {"bind": "/output", "mode": "rw"},
       },
       detach=False,
       remove=True,
       stdout=True,
       stderr=True,
   )
   ```
7. Read `{tmpdir}/output/{icd_config["outputs"]["metrics_file"]["path"].lstrip("/")}` → parse JSON → `metrics`.
8. Create tar.gz of weights directory `{tmpdir}/output/{icd_config["outputs"]["weights_path"]["path"].lstrip("/")}`.
9. Upload weights archive via `ctx.storage.save_bytes()` → `weights_blob_hash`.
10. Find `commit_id`: query the `runs` table for the step run in this workflow that produced the `export_blob_hash` (look in its `output_refs`).
11. Insert `model_versions` row: `blob_hash = weights_blob_hash`, `trained_on_commit_id`, `training_container_id`, `hyperparams`, `metrics`, `seed = hyperparams.get("seed")`.
12. Emit `events` row: `action = "model_version.created"`.
13. Return `{"model_version_id": "..."}`.

**Phase 2 upgrade:** use `detach=True`, poll every 5 seconds, stream logs to `runs.logs_blob_hash`, handle OOM and timeout as `failed` status with captured error.

---

## 11. CVAT Integration (Phase 2)

### 11.1 Push Flow (`human_review` gate step)

1. `human_review` step fires. Input: `annotation_revision_ids[]`.
2. Load samples from those revision IDs.
3. Call CVAT API:
   - `GET /api/projects?name={cvops_project_name}` — find or create CVAT project for this CVOps project.
   - `POST /api/tasks` — create task: `project_id`, `name`, label definitions from ontology (class names + colors).
   - `POST /api/tasks/{id}/data` — upload all sample images.
   - `POST /api/tasks/{id}/annotations` — upload pre-labels converted from `annotation_revision.payload` to CVAT JSON format.
4. Insert `labeling_jobs` row: `status = 'pushed'`, `cvat_task_id`, `cvat_job_ids`, `sample_count`, `annotation_revision_ids_in`.
5. Register webhook: `POST /api/webhooks` with `target_url = https://{our_host}/internal/cvat/webhook`, `events = ['update:job']`.
6. Raise `GateException({"labeling_job_id": "<uuid>"})` — workflow run transitions to `waiting`.

### 11.2 Pull Flow (webhook or polling)

**Webhook path:**
1. `POST /internal/cvat/webhook` receives `{event_type: "update:job", task_id, job_id, status}`.
2. Look up `labeling_jobs` by `cvat_task_id`. Check all `cvat_job_ids`; if all jobs have `status = 'completed'`:
3. `GET /api/tasks/{cvat_task_id}/annotations` — download completed annotations in CVAT JSON format.
4. Convert CVAT annotations to canonical geometry: CVAT label name → `class_key` lookup via ontology.
5. For each sample: insert new `annotation_revisions` row: `source = "human"`, `review_status = "accepted"`, `author_user_id = <cvat_user_map>`.
6. Update `labeling_jobs`: `status = 'completed'`, `completed_at = now()`, `annotation_revision_ids_out = [...]`.
7. Call `resume_workflow(workflow_run_id, {"annotation_revision_ids": [...]})`.

**Polling fallback** (runs every 5 minutes):
```sql
SELECT * FROM labeling_jobs
WHERE status = 'pushed'
  AND created_at < now() - interval '5 minutes'
```
For each row: call CVAT API to check job status. If all jobs complete, trigger the same pull flow as the webhook.

Phase 1: polling implemented as a simple async loop started at API startup. Phase 2: Celery beat task.

### 11.3 CVAT Geometry Conversion

**Push (CVOps → CVAT):**
```
geometry.type == "bbox":  coords [cx, cy, w, h] (normalized) →
  CVAT: x1 = (cx - w/2) * W, y1 = (cy - h/2) * H, x2 = (cx + w/2) * W, y2 = (cy + h/2) * H
  (where W, H = sample.width, sample.height)
```

**Pull (CVAT → CVOps):**
```
CVAT bbox [x1, y1, x2, y2] (absolute pixels) →
  cx = (x1 + x2) / (2 * W), cy = (y1 + y2) / (2 * H)
  w = (x2 - x1) / W, h = (y2 - y1) / H
```

### 11.4 CVAT Completion Signal

Webhook is primary. If webhook not received within 5 minutes of `labeling_jobs.created_at`, polling takes over. Both paths call the same `pull_and_ingest()` function. Idempotent: if annotations already ingested, second invocation is a no-op (check `labeling_jobs.status == 'completed'` at the top of the function).

---

## 12. Complete API Surface

All endpoints authenticated via `Authorization: Bearer <access_token>` unless noted. All list endpoints use cursor pagination (`?cursor=<opaque>&limit=<n>`). All write endpoints emit `events` rows. URL prefix: `/api/` (nginx proxies `/api/` to the FastAPI service).

### Auth (unauthenticated)

```
POST /auth/register
  body: {email, password, org_name?}
  → {access_token, refresh_token, user}

POST /auth/token
  body: OAuth2PasswordRequestForm {username, password}
  → {access_token, token_type: "bearer", refresh_token}

POST /auth/refresh
  body: {refresh_token}
  → {access_token, refresh_token}   (old refresh token revoked)

POST /auth/revoke
  body: {refresh_token}
  → 204

GET /auth/me
  → {id, email, org_id, role}
```

### Registry (read-only within org)

```
GET /registry/types?category=<category>
  → [{type_key, category, json_schema, ui_hints}, ...]

GET /registry/types/{type_key}
  → {type_key, category, json_schema, ui_hints}
```

### Orgs and Members

```
GET    /orgs/current                    → org
PATCH  /orgs/current                    body: {name?, settings?} → org
GET    /orgs/current/members            → [{user, role}, ...]
POST   /orgs/current/members            body: {email, role} → membership
PATCH  /orgs/current/members/{user_id}  body: {role} → membership
DELETE /orgs/current/members/{user_id}  → 204
```

### Projects

```
GET    /projects                    → [{id, name, task_type, org_id, created_at, ...}, ...]
POST   /projects                    body: {name, task_type, settings?} → project
GET    /projects/{id}               → project
PATCH  /projects/{id}               body: {name?, settings?} → project
DELETE /projects/{id}               → 204  (soft-delete)
```

### Data Sources

```
GET  /projects/{id}/data-sources
  → [{id, type, status, metadata, created_at, ...}, ...]

POST /projects/{id}/data-sources
  body: {type: "video"|"image_folder", metadata?}
  → {data_source, presigned_put_url}   (client uploads blob to presigned URL)

POST /projects/{id}/data-sources
  body: {type: "external_uri", external_uri}
  → data_source

POST /data-sources/{id}/confirm-upload
  body: {blob_hash}
  → data_source   (API verifies hash, inserts blobs row, sets status='uploaded')

GET  /data-sources/{id}    → data_source
DELETE /data-sources/{id}  → 204
```

### Samples

```
GET /projects/{id}/samples
  ?cursor=&source_id=&review_status=&split=&class_key=&limit=50
  → {items: [{id, blob_hash, thumbnail_url, width, height, frame_index, perceptual_hash, metadata, latest_revision_no}], next_cursor}

GET /samples/{id}
  → sample + latest annotation_revision

GET /samples/{id}/image-url
  → {url: "<presigned GET, 15-min TTL>"}

GET /samples/{id}/thumbnail-url
  → {url: "<presigned GET, 1-hr TTL>"}

GET /samples/{id}/annotations
  → [{revision_no, created_at, provenance, payload}, ...]   (full history)

POST /samples/{id}/annotations
  body: {payload, provenance}
  → annotation_revision   (manual editor creates new revision)
```

### Ontologies

```
GET  /projects/{id}/ontologies            → [ontology + label_classes, ...]
POST /projects/{id}/ontologies            body: {name} → ontology
GET  /ontologies/{id}                     → ontology + label_classes
POST /ontologies/{id}/classes             body: {class_key, display_name, color, sort_order} → label_class
PATCH /ontologies/{id}/classes/{class_id} body: {display_name?, color?, sort_order?} → label_class
DELETE /ontologies/{id}/classes/{class_id} → 204  (soft-delete; does not affect existing commits)
```

### Datasets and Versioning

```
GET  /projects/{id}/datasets              → [dataset, ...]
POST /projects/{id}/datasets              body: {name} → dataset
GET  /datasets/{id}                       → dataset + refs

GET  /datasets/{id}/commits?cursor=&ref=
  → [{id, parent_commit_id, stats, created_at, message, ontology_version}, ...]

GET  /datasets/{id}/commits/{commit_id}
  → commit + stats   (Cache-Control: immutable)

POST /datasets/{id}/commits
  body: {message, sample_ids, annotation_revision_ids, split_strategy, ontology_id, branch_name}
  → commit

GET  /datasets/{id}/refs                  → [ref, ...]
POST /datasets/{id}/refs                  body: {name, ref_type, target_commit_id} → ref
DELETE /datasets/{id}/refs/{ref_id}       → 204  (hard-delete, audited via events)

POST /datasets/{id}/merge
  body: {from_ref_id, into_ref_id, merge_policy}
  → {commit_id}

GET  /datasets/{id}/diff?from={commit_id}&to={commit_id}
  → {added: [sample_ids], removed: [sample_ids], changed: [sample_ids]}

POST /projects/{id}/dataset-links
  body: {dataset_id, pinned_commit_id?, ref_id?}
  → link

PATCH /dataset-links/{id}
  body: {pinned_commit_id?, ref_id?}
  → link

DELETE /dataset-links/{id}  → 204
```

### Workflows and Runs

```
GET    /projects/{id}/workflows
  → [workflow, ...]

POST   /projects/{id}/workflows
  body: {name, definition}   (validates all step configs against registry at save time)
  → workflow

GET    /workflows/{id}    → workflow
PATCH  /workflows/{id}    body: {name?, definition?} → workflow  (bumps version)
DELETE /workflows/{id}    → 204

POST /workflows/{id}/runs
  body: {params: {...}}
  → run   (execution starts in background via BackgroundTasks)

GET /runs/{id}
  → run + child step runs

GET /runs/{id}/events
  → [{id, action, entity_type, entity_id, payload, created_at}, ...]

GET /runs/{id}/events/stream
  → SSE stream; each event: "data: <json>\n\n"

POST /runs/{id}/cancel    → 204
POST /runs/{id}/retry     → run  (increments attempt; re-executes from last failed step)

POST /runs/{id}/gates/{step_id}/resolve
  body: {resolution: "proceed"|"skip"}
  → 204   (calls resume_workflow)
```

### Models

```
GET /projects/{id}/models
  → [{id, trained_on_commit_id, training_container_id, base_model, hyperparams, metrics, created_at}, ...]

GET /models/{id}
  → model_version + trained_on_commit stats

GET /models/{id}/weights-url
  → {url: "<presigned GET, 1-hr TTL>"}
```

### Training Containers

```
GET    /projects/{id}/training-containers      → [training_container, ...]
POST   /projects/{id}/training-containers
  body: {name, image, icd_config, description?}
  → training_container

GET    /training-containers/{id}    → training_container
PATCH  /training-containers/{id}    body: {name?, image?, icd_config?} → training_container
DELETE /training-containers/{id}    → 204

POST /training-containers/{id}/validate
  body: {icd_config}
  → {valid: true} | {valid: false, errors: [...]}
```

### Internal (workers and CVAT webhook)

```
POST /internal/cvat/webhook
  body: {event_type, task_id, job_id, status, ...}
  → 204

GET /internal/health
  → {status: "ok"|"degraded", db: "ok"|"error", minio: "ok"|"error", redis: "ok"|"error"}
```

### SSE Stream Implementation

`GET /runs/{id}/events/stream` uses FastAPI `StreamingResponse` with `media_type = "text/event-stream"`. The generator:
1. Sends all existing events for the run immediately (catchup).
2. Polls the `events` table every 1 second for rows with `entity_id = run_id` and `id > last_seen_id`.
3. Yields each new row as `data: <json>\n\n`.
4. Closes when the run reaches a terminal state (`succeeded`, `failed`, `canceled`).

---

## 13. Auth Flow

### Token Lifecycle

**Access token:**
- JWT, HS256, 15-minute TTL.
- Payload: `{sub: user_id, org_id, role, exp}`.
- Secret: `settings.JWT_SECRET` (minimum 32 characters, from env var).

**Refresh token:**
- UUID v4, stored as `bcrypt(raw_token)` in `refresh_tokens.token_hash`.
- 7-day TTL (`expires_at`).
- Rotation on use: new access token + new refresh token issued; old refresh token `revoked = true`.

**Worker service tokens:**
- Long-lived JWTs (1-year TTL), `sub = "worker"`.
- Injected as `WORKER_TOKEN` environment variable in Docker Compose.
- `get_current_user` dependency accepts these and returns a synthetic worker user object.

### FastAPI Dependency Chain

```python
# services/api/src/cvops_api/core/auth.py

async def get_current_user(
    token: str = Depends(oauth2_scheme),
    session: AsyncSession = Depends(get_session)
) -> User:
    try:
        payload = jose.jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
    except jose.JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = await session.get(User, payload["sub"])
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user

# services/api/src/cvops_api/core/rbac.py

def require_project_access(min_role: str = "viewer"):
    async def dep(
        project_id: str,
        current_user: User = Depends(get_current_user),
        session: AsyncSession = Depends(get_session)
    ) -> Project:
        project = await session.get(Project, project_id)
        if not project or project.deleted_at is not None:
            raise HTTPException(status_code=404)
        membership = await get_membership(session, project.org_id, current_user.id)
        if not membership or not has_role(membership.role, min_role):
            raise HTTPException(status_code=403)
        return project
    return dep
```

Role hierarchy (inclusive): `owner > maintainer > annotator > viewer`. `has_role(actual, required)` returns `True` if `actual` is at or above `required` in the hierarchy.

---

## 14. Frontend Architecture

### 14.1 Page Routing (React Router v6)

```
/                              → redirect to /projects
/login                         → Login
/register                      → Register
/projects                      → Projects (list + create)
/projects/:id                  → Project (overview: data sources, recent runs, model count)
/projects/:id/data-sources     → DataSources (upload + ingest status)
/projects/:id/samples          → SampleBrowser (thumbnail grid, filters)
/projects/:id/datasets         → Datasets (list datasets)
/datasets/:id                  → DatasetView (commit graph, refs, stats)
/datasets/:id/commits/:cid     → CommitDetail (members, stats, diff)
/projects/:id/workflows        → Workflows (list + create)
/workflows/:id                 → WorkflowBuilder (React Flow canvas)
/runs/:id                      → RunView (live timeline, step cards)
/projects/:id/models           → Models (list model versions)
/models/:id                    → ModelDetail (metrics, trained_on_commit link, weights download)
/projects/:id/settings         → ProjectSettings (ontology editor, training containers, members)
```

### 14.2 Axios Client and Auth Interceptor

Location: `services/frontend/src/lib/client.ts`

```typescript
import axios from 'axios'

const client = axios.create({ baseURL: '/api' })

client.interceptors.request.use((config) => {
  const token = localStorage.getItem('access_token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

client.interceptors.response.use(
  (r) => r,
  async (error) => {
    if (error.response?.status === 401 && !error.config._retry) {
      error.config._retry = true
      const refreshToken = localStorage.getItem('refresh_token')
      const { data } = await axios.post('/api/auth/refresh', { refresh_token: refreshToken })
      localStorage.setItem('access_token', data.access_token)
      localStorage.setItem('refresh_token', data.refresh_token)
      error.config.headers.Authorization = `Bearer ${data.access_token}`
      return client(error.config)
    }
    return Promise.reject(error)
  }
)

export default client
```

### 14.3 TanStack Query Key Conventions

```typescript
// api/projects.ts
export const useProjects = () =>
  useQuery({ queryKey: ['projects'], queryFn: () => client.get('/projects').then(r => r.data) })

export const useCreateProject = () =>
  useMutation({
    mutationFn: (data) => client.post('/projects', data).then(r => r.data),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['projects'] })
  })

// api/runs.ts
export const useRun = (id: string) =>
  useQuery({
    queryKey: ['runs', id],
    queryFn: () => client.get(`/runs/${id}`).then(r => r.data),
    refetchInterval: (data) =>
      data?.status === 'running' || data?.status === 'waiting' ? 2000 : false
  })

export const useRunEvents = (id: string) =>
  useQuery({
    queryKey: ['runs', id, 'events'],
    queryFn: () => client.get(`/runs/${id}/events`).then(r => r.data)
  })
```

### 14.4 Schema-Driven Step Config Forms

Location: `services/frontend/src/components/workflow/StepConfigForm.tsx`

```typescript
import { Form } from '@rjsf/core'
import validator from '@rjsf/validator-ajv8'
import { useQuery } from '@tanstack/react-query'
import client from '../../lib/client'

interface Props {
  typeKey: string
  value: Record<string, unknown>
  onChange: (formData: Record<string, unknown>) => void
}

export function StepConfigForm({ typeKey, value, onChange }: Props) {
  const { data: schema } = useQuery({
    queryKey: ['registry', typeKey],
    queryFn: () => client.get(`/registry/types/${typeKey}`).then(r => r.data)
  })
  if (!schema) return <div className="animate-pulse h-32 bg-gray-100 rounded" />
  return (
    <Form
      schema={schema.json_schema}
      uiSchema={schema.ui_hints ?? {}}
      formData={value}
      onChange={({ formData }) => onChange(formData)}
      validator={validator}
      liveValidate
    />
  )
}
```

Every step in the workflow builder uses this component. Adding a new step type to the registry makes it immediately configurable in the UI with zero frontend changes.

### 14.5 Workflow Canvas (React Flow)

Location: `services/frontend/src/components/workflow/Canvas.tsx`

- Each step in `definition.steps` maps to a `StepNode` React Flow node.
- Each entry in `definition.edges` maps to a React Flow edge.
- `StepNode` renders: `type_key` badge, condensed config summary, input/output handle points.
- "Configure" button on a node opens a modal with `<StepConfigForm typeKey={node.data.type} ... />`.
- On save: serialize React Flow nodes + edges back to `definition` JSON, call `PATCH /workflows/{id}`.
- `StepPalette` sidebar lists all `step.*` types from `GET /registry/types?category=step`; drag a palette item onto canvas to add a new step node.

### 14.6 SSE Run Monitoring

Location: `services/frontend/src/pages/RunView.tsx`

```typescript
useEffect(() => {
  const es = new EventSource(`/api/runs/${id}/events/stream`)
  es.onmessage = (e) => {
    const event = JSON.parse(e.data)
    queryClient.setQueryData(
      ['runs', id, 'events'],
      (old: Event[]) => [...(old ?? []), event]
    )
    queryClient.invalidateQueries({ queryKey: ['runs', id] })
  }
  es.onerror = () => es.close()
  return () => es.close()
}, [id])
```

### 14.7 Zustand UI Store

Location: `services/frontend/src/store/ui.ts`

```typescript
interface UIStore {
  activeProjectId: string | null
  sidebarOpen: boolean
  setActiveProject: (id: string) => void
  toggleSidebar: () => void
}
```

Persisted to `localStorage` for `activeProjectId`.

---

## 15. Repository Structure

```
cvops/
├── .env.example
├── .gitignore
├── docker-compose.yml
├── docker-compose.dev.yml           # hot-reload volume mounts
│
├── nginx/
│   └── nginx.conf                   # /api/ → api:8000 | / → frontend:3000 | /minio-console/ → minio:9001
│
├── packages/
│   │
│   ├── api/
│   │   ├── Dockerfile               # multi-stage: python:3.12 build → python:3.12-slim runtime
│   │   ├── pyproject.toml           # deps: fastapi, sqlalchemy[asyncio], asyncpg, alembic,
│   │   │                            #   pydantic-settings, python-jose[cryptography], passlib[bcrypt],
│   │   │                            #   boto3, jsonschema, pillow, ffmpeg-python, docker, redis, httpx
│   │   ├── alembic.ini
│   │   ├── alembic/
│   │   │   ├── env.py
│   │   │   └── versions/
│   │   │       ├── 001_initial.py
│   │   │       ├── 002_auth_tables.py
│   │   │       ├── 003_blobs_events_schemas.py
│   │   │       ├── 004_projects_data_sources.py
│   │   │       ├── 005_samples_ontologies.py
│   │   │       ├── 006_annotation_revisions.py
│   │   │       ├── 007_versioning.py
│   │   │       ├── 008_workflows_runs.py
│   │   │       ├── 009_models_containers.py
│   │   │       └── 010_labeling_jobs.py
│   │   └── src/
│   │       └── cvops_api/
│   │           ├── main.py              # FastAPI app; lifespan: DB + storage + registry init; CORS; router include
│   │           ├── config.py            # pydantic-settings: DATABASE_URL, MINIO_*, REDIS_URL, JWT_SECRET, WORKER_TOKEN
│   │           ├── db/
│   │           │   ├── base.py          # EntityBase SQLAlchemy mixin (G1 spine)
│   │           │   ├── session.py       # async_sessionmaker, get_session dependency
│   │           │   └── models/
│   │           │       ├── blobs.py
│   │           │       ├── auth.py      # Org, User, Membership, RefreshToken
│   │           │       ├── projects.py  # Project, DataSource
│   │           │       ├── data_items.py # DataItem (replaces Sample — domain-agnostic)
│   │           │       ├── ontologies.py# Ontology, LabelClass
│   │           │       ├── annotations.py # AnnotationRevision
│   │           │       ├── versioning.py  # Dataset, Commit, CommitSample, Ref, ProjectDatasetLink
│   │           │       ├── workflows.py   # Workflow
│   │           │       ├── runs.py        # Run
│   │           │       ├── models.py      # ModelVersion, TrainingContainer
│   │           │       └── labeling.py    # LabelingJob
│   │           ├── core/
│   │           │   ├── registry.py      # singleton Registry; register(), resolve(), validate_config(), list_by_category()
│   │           │   ├── storage.py       # StorageBackend ABC + MinIOBackend implementation
│   │           │   ├── auth.py          # JWT encode/decode, get_current_user FastAPI dependency
│   │           │   ├── rbac.py          # require_project_access dependency
│   │           │   └── audit.py         # emit_event() coroutine
│   │           ├── routers/
│   │           │   ├── auth.py
│   │           │   ├── registry.py
│   │           │   ├── orgs.py
│   │           │   ├── projects.py
│   │           │   ├── data_sources.py
│   │           │   ├── samples.py
│   │           │   ├── ontologies.py
│   │           │   ├── datasets.py      # datasets + commits + refs + merge + diff + links
│   │           │   ├── workflows.py
│   │           │   ├── runs.py          # runs + cancel + retry + gate resolve + SSE stream
│   │           │   ├── models.py
│   │           │   ├── training_containers.py
│   │           │   └── internal.py      # /internal/cvat/webhook, /internal/health
│   │           └── engine/
│   │               ├── step.py          # StepContext, GateException, Step ABC
│   │               ├── executor.py      # execute_workflow(), resume_workflow()
│   │               └── ref_resolver.py  # evaluates $steps.<id>.outputs.<name> references
│   │
│   ├── workers/                         # Phase 2 — three separate worker services
│   │   │                                # all import from packages/steps (shared lib)
│   │   │                                # all use same polling loop pattern
│   │   │
│   │   ├── preprocessing/               # handles: extract_frames, auto_label, commit_dataset, export_*
│   │   │   ├── Dockerfile
│   │   │   ├── pyproject.toml           # deps: asyncpg, redis, boto3; depends on cvops_steps
│   │   │   └── src/cvops_worker_preprocessing/
│   │   │       ├── main.py              # polling loop: SELECT FOR UPDATE SKIP LOCKED on runs table
│   │   │       │                        # filters step_type IN ('step.extract_frames', 'step.auto_label', ...)
│   │   │       └── consumer.py          # Redis Stream XREADGROUP consumer; on message → claim run in PG
│   │   │
│   │   ├── labeling/                    # handles: human_review (CVAT push/pull/webhook)
│   │   │   ├── Dockerfile
│   │   │   ├── pyproject.toml           # deps: asyncpg, redis, httpx; depends on cvops_steps
│   │   │   └── src/cvops_worker_labeling/
│   │   │       ├── main.py              # polling loop for labeling queue
│   │   │       └── consumer.py          # Redis Stream consumer for labeling stream
│   │   │
│   │   └── training/                    # handles: train, evaluate
│   │       ├── Dockerfile
│   │       ├── pyproject.toml           # deps: asyncpg, redis, boto3, docker; depends on cvops_steps
│   │       └── src/cvops_worker_training/
│   │           ├── main.py              # polling loop for training queue
│   │           └── consumer.py          # Redis Stream consumer for training stream
│   │           # NOTE: this is the ONLY worker with Docker socket access
│   │
│   ├── steps/                           # shared library — imported by all workers, NOT a running service
│   │   ├── pyproject.toml               # depends on cvops_api (Step contract, StorageBackend, DB models)
│   │   └── src/cvops_steps/
│   │       ├── __init__.py              # register_all() — calls registry.register() for all steps
│   │       ├── extract_frames.py        # queue = "preprocessing"
│   │       ├── auto_label.py            # queue = "preprocessing"
│   │       ├── human_review.py          # queue = "labeling"  (gate step)
│   │       ├── commit_dataset.py        # queue = "preprocessing"
│   │       ├── export_yolo.py           # queue = "preprocessing"
│   │       ├── train.py                 # queue = "training"
│   │       └── schemas/
│   │           ├── extract_frames.json
│   │           ├── auto_label.json
│   │           ├── commit_dataset.json
│   │           ├── export_yolo.json
│   │           └── train.json
│   │
│   └── frontend/
│       ├── Dockerfile                   # node:20 build → nginx:alpine serve
│       ├── package.json
│       ├── vite.config.ts
│       ├── tailwind.config.ts
│       ├── tsconfig.json                # strict: true
│       └── src/
│           ├── main.tsx
│           ├── App.tsx                  # Router + QueryClientProvider + auth guard
│           ├── lib/
│           │   ├── client.ts            # axios instance, token interceptor, refresh logic
│           │   └── queryClient.ts       # TanStack QueryClient singleton
│           ├── api/                     # one file per resource; TanStack Query hooks
│           │   ├── auth.ts
│           │   ├── projects.ts
│           │   ├── samples.ts
│           │   ├── datasets.ts
│           │   ├── workflows.ts
│           │   ├── runs.ts
│           │   ├── models.ts
│           │   ├── registry.ts
│           │   └── training-containers.ts
│           ├── store/
│           │   └── ui.ts                # Zustand: activeProjectId, sidebarOpen
│           ├── components/
│           │   ├── ui/                  # shadcn/ui generated components
│           │   ├── workflow/
│           │   │   ├── Canvas.tsx       # React Flow canvas
│           │   │   ├── StepNode.tsx     # React Flow custom node
│           │   │   ├── StepConfigForm.tsx  # rjsf form from registry schema
│           │   │   └── StepPalette.tsx  # drag-from sidebar of step types
│           │   ├── runs/
│           │   │   ├── RunTimeline.tsx
│           │   │   ├── StepRunCard.tsx
│           │   │   └── GateResolutionBanner.tsx  # "42 CVAT jobs pending — Open in CVAT →"
│           │   ├── dataset/
│           │   │   ├── CommitGraph.tsx  # React Flow or SVG; shows commit DAG
│           │   │   ├── SampleGrid.tsx   # cursor-paginated thumbnail grid
│           │   │   └── CommitStats.tsx  # by_split / by_class bar charts
│           │   └── layout/
│           │       ├── Sidebar.tsx
│           │       └── Header.tsx
│           └── pages/
│               ├── Login.tsx
│               ├── Register.tsx
│               ├── Projects.tsx
│               ├── Project.tsx
│               ├── DataSources.tsx
│               ├── SampleBrowser.tsx
│               ├── Datasets.tsx
│               ├── DatasetView.tsx
│               ├── CommitDetail.tsx
│               ├── Workflows.tsx
│               ├── WorkflowBuilder.tsx
│               ├── RunView.tsx
│               ├── Models.tsx
│               ├── ModelDetail.tsx
│               └── ProjectSettings.tsx
```

---

## 16. Docker Compose Service Map

```yaml
services:
  postgres:
    image: postgres:16
    ports: ["5432:5432"]
    environment:
      POSTGRES_DB: cvops
      POSTGRES_USER: cvops
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes: [postgres_data:/var/lib/postgresql/data]
    healthcheck:
      test: ["CMD", "pg_isready", "-U", "cvops"]
      interval: 5s
      retries: 10

  minio:
    image: minio/minio:latest
    command: server /data --console-address :9001
    ports: ["9000:9000", "9001:9001"]
    environment:
      MINIO_ROOT_USER: ${MINIO_ROOT_USER}
      MINIO_ROOT_PASSWORD: ${MINIO_ROOT_PASSWORD}
    volumes: [minio_data:/data]
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/live"]
      interval: 5s
      retries: 10

  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
    volumes: [redis_data:/data]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      retries: 10

  api:
    build: services/api
    ports: ["8000:8000"]
    depends_on:
      postgres: {condition: service_healthy}
      minio: {condition: service_healthy}
      redis: {condition: service_healthy}
    environment:
      DATABASE_URL: postgresql+asyncpg://cvops:${POSTGRES_PASSWORD}@postgres:5432/cvops
      MINIO_ENDPOINT: http://minio:9000
      MINIO_ACCESS_KEY: ${MINIO_ROOT_USER}
      MINIO_SECRET_KEY: ${MINIO_ROOT_PASSWORD}
      MINIO_BUCKET: cvops-blobs
      REDIS_URL: redis://redis:6379/0
      JWT_SECRET: ${JWT_SECRET}
      WORKER_TOKEN: ${WORKER_TOKEN}
    # NO docker socket here — training dispatch is worker-training's responsibility

  # ── Workers (Phase 2) ───────────────────────────────────────────────────────
  # Phase 1: steps run inside API via BackgroundTasks (no workers needed yet)
  # Phase 2: uncomment workers below

  worker-preprocessing:  # Phase 2
    build: services/workers/preprocessing
    depends_on:
      postgres: {condition: service_healthy}
      redis: {condition: service_healthy}
      minio: {condition: service_healthy}
    environment:
      DATABASE_URL: postgresql+asyncpg://cvops:${POSTGRES_PASSWORD}@postgres:5432/cvops
      REDIS_URL: redis://redis:6379/0
      MINIO_ENDPOINT: http://minio:9000
      MINIO_ACCESS_KEY: ${MINIO_ROOT_USER}
      MINIO_SECRET_KEY: ${MINIO_ROOT_PASSWORD}
      REDIS_STREAM: preprocessing
      WORKER_TOKEN: ${WORKER_TOKEN}
    deploy:
      replicas: 2   # scale horizontally — PG SKIP LOCKED handles concurrency

  worker-labeling:  # Phase 2
    build: services/workers/labeling
    depends_on:
      postgres: {condition: service_healthy}
      redis: {condition: service_healthy}
    environment:
      DATABASE_URL: postgresql+asyncpg://cvops:${POSTGRES_PASSWORD}@postgres:5432/cvops
      REDIS_URL: redis://redis:6379/0
      REDIS_STREAM: labeling
      CVAT_URL: http://cvat:8080
      CVAT_USERNAME: ${CVAT_USERNAME}
      CVAT_PASSWORD: ${CVAT_PASSWORD}
      WORKER_TOKEN: ${WORKER_TOKEN}

  worker-training:  # Phase 2
    build: services/workers/training
    depends_on:
      postgres: {condition: service_healthy}
      redis: {condition: service_healthy}
      minio: {condition: service_healthy}
    environment:
      DATABASE_URL: postgresql+asyncpg://cvops:${POSTGRES_PASSWORD}@postgres:5432/cvops
      REDIS_URL: redis://redis:6379/0
      MINIO_ENDPOINT: http://minio:9000
      MINIO_ACCESS_KEY: ${MINIO_ROOT_USER}
      MINIO_SECRET_KEY: ${MINIO_ROOT_PASSWORD}
      REDIS_STREAM: training
      WORKER_TOKEN: ${WORKER_TOKEN}
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock  # ONLY this worker needs Docker socket

  frontend:
    build: services/frontend
    ports: ["3000:80"]

  nginx:
    image: nginx:alpine
    ports: ["80:80"]
    volumes: [./nginx/nginx.conf:/etc/nginx/nginx.conf:ro]
    depends_on: [api, frontend]

  cvat:  # Phase 2
    image: cvat/server:latest
    ports: ["8080:8080"]

volumes:
  postgres_data:
  minio_data:
  redis_data:
```

**Redis Stream names (one per worker type):**
```
preprocessing   ← jobs for worker-preprocessing
labeling        ← jobs for worker-labeling
training        ← jobs for worker-training
```

**Redis Stream message format (thin — all state lives in PG):**
```json
{
  "job_id":    "uuid of the runs row",
  "step_type": "step.extract_frames",
  "queue":     "preprocessing"
}
```
Workers read `job_id`, look up full config and inputs from the `runs` row in PostgreSQL.

**Orphan recovery** — on each worker startup and every 60 seconds:
```sql
SELECT id FROM runs
WHERE status = 'pending'
  AND step_type IN (<this worker's step types>)
  AND created_at < now() - interval '30 seconds'
```
Re-enqueue any found jobs into the Redis Stream. Handles Redis restart/message loss.

**API entrypoint script** (`services/api/entrypoint.sh`):
```sh
#!/bin/sh
set -e
alembic upgrade head
python -c "from cvops_steps import register_all; from cvops_api.core.registry import registry; register_all()"
exec uvicorn cvops_api.main:app --host 0.0.0.0 --port 8000
```

---

## 17. Testing Strategy

### Unit Tests

**Step tests:** Each `Step` subclass tested in isolation with fixture blobs and fixture DB state. Test that `run()` returns the correct output shape and that the correct rows are inserted.

**Registry tests:** `register(MockStep()); assert registry.resolve('mock.step') is not None`. Test `validate_config()` raises on bad config and passes on valid config.

**StorageBackend tests:** Use `moto` library to mock the S3/MinIO API for unit tests. Verify `save_bytes()` produces the correct hash, uploads to the correct path, and inserts a `blobs` row. Verify `get_presigned_get()` returns a URL containing the correct key.

**CAS tests (`commit_dataset`):** Mock a concurrent writer that advances the branch head between the `SELECT` and the `UPDATE`. Assert the retry path fires and succeeds. Assert that after 3 failures a `CommitConflictError` is raised.

**Immutability enforcement:** Any attempt to `UPDATE` or `DELETE` a commit row or annotation_revision row must raise an error. Tested by direct SQLAlchemy call.

### Integration Tests

Use `testcontainers-python` to spin up real PostgreSQL 16 and real MinIO for each integration test session.

**Executor pipeline test:** Run the synchronous executor against a sequence of 3 mock steps. Assert `runs` table rows at each transition (pending → running → succeeded). Assert `events` rows for each transition. Assert `output_refs` correctly thread between steps.

**Full path test:** Real JPEG frames → `extract_frames` → `auto_label` (with bootstrap YOLO weights fixture) → `commit_dataset` → `export_yolo`. Assert the final tar.gz archive extracts to a valid YOLO layout with correct `data.yaml`, correct image file count, and label files with normalized coordinates within [0,1].

### API Tests

`pytest` + `httpx.AsyncClient` with FastAPI test client.

- **Auth flow:** register → login → use access token → refresh → revoke → verify old access token rejected.
- **Dataset versioning:** create dataset → create two commits → verify CAS branch advance → merge with `human_over_model` policy → assert merge commit has two parents and correct sample union.
- **Pagination:** insert 200 samples → paginate through all with cursor → assert total count matches and no sample appears twice.

### Property Tests (`hypothesis`)

- **CAS correctness:** under concurrent simulated writers, the branch head always ends up pointing at a commit in the correct parent chain.
- **Merge union correctness:** the merged commit's `commit_samples` contains exactly the union of both parent commits' sample sets.
- **Immutability:** for any `commit_id` that exists, any SQLAlchemy `UPDATE` on that row must raise an integrity violation.
- **Hash idempotency:** `save_bytes(data)` called twice with the same bytes produces the same hash and does not create a duplicate `blobs` row.

---

## 18. Resolved Design Decisions

These are final. Do not reopen them during implementation.

| # | Decision | Resolution |
|---|---|---|
| 1 | Hash algorithm and blob key scheme | SHA-256, stored as `sha256:<64-hex>`. MinIO path: `blobs/{hash[7:9]}/{hash[9:]}` |
| 2 | Commit membership storage | `commit_samples` join table (not packed manifest blob). Add `manifest_blob_hash` column later if a project exceeds 5M samples. |
| 3 | Default split strategy | `by_source_group` is the enforced default for video-derived data. `random_seeded` requires explicit opt-in with a UI warning. |
| 4 | Merge conflict default policy | `human_over_model`. Conflict between two human-reviewed different-content revisions → escalate to `manual`. |
| 5 | CVAT completion signal | Webhook primary. Polling fallback every 5 minutes if webhook not received. |
| 6 | Storage backend Phase 1 | MinIO on-prem. S3/GCS swap via config only (no code change). |
| 7 | Annotation geometry schema | Normalized `{type, coords}`. `type ∈ {bbox, polygon, oriented_bbox, keypoints}`. `bbox` coords `[cx, cy, w, h]`, normalized 0.0–1.0, origin top-left. Locked now. |
| 8 | Soft-delete and GC retention | 30-day soft-delete window. Pinned commits never GC-eligible. Orphan blobs: 30-day minimum age, weekly sweep. Retention windows stored in `orgs.settings`. |
| 9 | MLflow integration | No integration in Phase 0 or Phase 1. Phase 2: optional `mlflow_run_id` column on `model_versions`, `mlflow_tracking_uri` in ICD config. |
| 10 | MVP scope cut line | Phase 1 ends when the full pipeline (video → extract → auto-label → commit → export → Docker train → model_version) works end-to-end in the browser. CVAT integration is Phase 2. |

---

## 19. Phased Build Plan

### Phase 0 — Foundations (Yehuda solo, ~1 week)

Strictly ordered. Each task has a single "done" condition.

**D1. Repo init.**
Create monorepo root. `docker-compose.yml` with service stubs (images pinned, no logic yet). `.env.example` with all env var names. `.gitignore`.
Done: `docker compose up postgres minio redis` all reach healthy state.

**D2. SQLAlchemy + G1 mixin + first migration.**
`services/api/src/cvops_api/db/base.py`: `EntityBase` declarative mixin with all G1 columns. `alembic/versions/001_initial.py`: empty migration confirming plumbing. `db/session.py`: `async_sessionmaker`, `get_session` dependency.
Done: `alembic upgrade head` succeeds; `alembic current` shows `001`.

**D3. All migrations (002–010).**
Write one migration file per group. Apply all FK constraints, all CHECK constraints, all indexes from §5.
Done: all tables visible in `psql \dt`; all FKs enforced; `\d commit_samples` shows composite PK and both FKs.

**D4. MinIO + StorageBackend.**
`core/storage.py`: `StorageBackend` ABC + `MinIOBackend`. Methods: `save_bytes`, `save_stream`, `get_presigned_get`, `get_presigned_put`, `get_bytes`, `delete_blob`. Bucket auto-create on initialization. CORS policy set.
Done: test script uploads a 10KB PNG, downloads via presigned URL, compares bytes, verifies `blobs` row exists with correct `sha256:` hash.

**D5. FastAPI app skeleton + health endpoint.**
`main.py` with lifespan (DB engine + StorageBackend init), CORS middleware, `GET /internal/health`. `Dockerfile` (multi-stage). Wire service into `docker-compose.yml`.
Done: `curl http://localhost:8000/internal/health` returns `{"status": "ok", "db": "ok", "minio": "ok", "redis": "ok"}` from inside Docker.

**D6. Auth endpoints.**
Full `POST /auth/register`, `POST /auth/token`, `POST /auth/refresh`, `POST /auth/revoke`, `GET /auth/me`. JWT generation, bcrypt hashing, `refresh_tokens` table, token rotation. `get_current_user` dependency.
Done: complete auth flow works: register a user, obtain access token, use it on `/auth/me`, refresh it, revoke it, verify revoked token rejected.

**D7. Registry.**
`core/registry.py` with in-memory dict + `type_schemas` DB sync. `GET /registry/types`, `GET /registry/types/{type_key}`. `validate_config()` using `jsonschema`.
Done: endpoints return empty list (no steps registered yet) without errors. `validate_config('nonexistent', {})` raises `KeyError`.

**D8. Audit helper.**
`core/audit.py`: `emit_event(session, actor_id, actor_type, entity_type, entity_id, action, payload)` coroutine. Unit test confirms row is inserted in `events` table.
Done: unit test passes; `events` row visible in DB.

**D9. React shell.**
Vite + React 18 + TypeScript strict. TailwindCSS + shadcn/ui init (`npx shadcn-ui@latest init`). TanStack Query v5 `QueryClientProvider`. Zustand store stub. React Router v6 with `/login` and `/projects` routes. Axios `client.ts` with token interceptor. Placeholder `Projects` page that calls `GET /projects`. `Dockerfile` (node:20 build → nginx:alpine). Wire into `docker-compose.yml` and `nginx.conf`.
Done: browser at `http://localhost` shows placeholder Projects page; `/internal/health` call visible in browser network tab with 200 status.

**Phase 0 complete:** All infrastructure wired. All 21 DB tables exist with all constraints. Auth works. Registry empty but functional. Frontend shell live.

---

### Phase 1 — MVP (parallel tracks, ~2–3 weeks)

Converges to: upload video → configure workflow in browser → run → view model_version record.

**E1. Projects + orgs CRUD (Yehuda).**
All project, org, and membership endpoints. Creating a user via `/auth/register` auto-creates an org and adds the user as `owner`. Emit `events` on create/delete.
Done: `GET /projects` returns the created project; `GET /orgs/current/members` returns the owner.

**E2. Data source + presigned upload (Yehuda).**
`POST /projects/{id}/data-sources` returns presigned PUT URL. `POST /data-sources/{id}/confirm-upload` verifies hash and sets `status = 'uploaded'`.
Done: upload a test video via curl using the presigned URL; `data_sources` row has `blob_hash` matching the SHA-256 of the file; blob visible in MinIO.

**E3. Step contract + synchronous executor (Itai).**
`engine/step.py`, `engine/executor.py`, `engine/ref_resolver.py`. Unit test with two mock steps: mock step 1 returns `{"foo": "bar"}`; mock step 2 takes `$steps.s1.outputs.foo` as input and returns `{"baz": "qux"}`. Assert both `runs` rows created with correct `status = 'succeeded'` and `output_refs`.
Done: two-step mock chain produces correct `runs` rows.

**E4. `extract_frames` step (Nati/Yahav).**
Depends on D4 (storage) + E3 (step contract).
Done: running the step on a 30-second test video with `interval_seconds=2` produces ~15 `samples` rows, all with `blob_hash` pointing to real JPEGs in MinIO and `thumbnail_hash` pointing to 256×256 thumbnails. Near-duplicate frames from a static scene are deduplicated.

**E5. `auto_label` step (Nati/Yahav).**
Depends on E4 outputs as inputs, plus an existing `model_versions` row (bootstrap: use a pre-trained YOLOv8n checkpoint uploaded manually to MinIO).
Done: step produces `annotation_revisions` rows with `payload` containing valid normalized bbox geometry and `provenance.source = "model"`.

**E6. `commit_dataset` step (Yehuda).**
Depends on E4 + E5 outputs. Includes CAS retry test.
Done: concurrent-write CAS test passes. After step execution: `commits` row exists with `stats` populated; `commit_samples` rows exist for all samples; `refs` row for `main` branch points at the new commit.

**E7. `export_yolo` step (Nati/Yahav).**
Depends on E6. Idempotency check implemented.
Done: produced tar.gz extracts to valid YOLO layout with `data.yaml`, `images/train/`, `labels/train/`. Re-running the step returns the same `export_blob_hash` without re-uploading.

**E8. `training_containers` API + `train` step.**
Yehuda: CRUD endpoints + ICD config validation endpoint. Nati/Yahav: `train` step.
Done (API): `POST /projects/{id}/training-containers` with a valid ICD config returns the row; invalid config returns 422 with validation errors.
Done (step): running against a minimal Docker container image that writes a fake `metrics.json` and fake weights produces a `model_versions` row with correct `trained_on_commit_id` and `metrics`.

**E9. Workflows + runs API (Itai executor wiring + Yehuda HTTP layer).**
Yehuda: `POST /projects/{id}/workflows`, `POST /workflows/{id}/runs` (calls `execute_workflow` in `BackgroundTasks`), `GET /runs/{id}`, `GET /runs/{id}/events`, `GET /runs/{id}/events/stream`.
Itai: wire executor into the run endpoint; ensure workflow run transitions to `succeeded` after all step runs succeed.
Done: `POST /workflows/{id}/runs` returns a run with `status = 'pending'`; after a few seconds `GET /runs/{id}` returns `status = 'succeeded'`; all child step runs visible.

**E10. Sample browser + thumbnails (Yehuda).**
Cursor-paginated `GET /projects/{id}/samples` with filters. `GET /samples/{id}/thumbnail-url` returning a presigned GET URL.
Done: 50-thumbnail grid loads in under 1 second; cursor pagination returns non-overlapping pages.

**E11. Frontend core pages (Yehuda + optional Nati/Yahav).**
Pages: `Projects`, `Project`, `DataSources` (upload form), `SampleBrowser` (thumbnail grid with filters), `WorkflowBuilder` (Phase 1: step list with rjsf forms, no React Flow canvas yet — full canvas is Phase 2), `RunView` (status + step cards, SSE live updates), `Models`.
Done: a user with only a browser and a video file can complete the full pipeline without using curl.

**E12. Full docker-compose integration (Yehuda).**
`entrypoint.sh` runs `alembic upgrade head` then `register_all()` then `uvicorn`. All services healthy on `docker compose up` from a clean checkout.
Done: `docker compose up --build` from a fresh clone; complete pipeline demo runs in browser.

**Phase 1 complete:** Working end-to-end demo. Video → extract → auto-label → commit → export → Docker train → model_version, all in the browser.

---

### Phase 2 — Human Loop + Real Versioning UX (~2–3 weeks)

- Add CVAT to `docker-compose.yml`. Implement `human_review` step: push images + pre-labels to CVAT, insert `labeling_jobs` row, raise `GateException`.
- Implement `POST /internal/cvat/webhook` + polling fallback. Pull annotations, insert new revisions, call `resume_workflow`.
- `GateResolutionBanner` in `RunView`: shows "N CVAT jobs pending — Open in CVAT →" with link.
- Replace Phase 1 form-based `WorkflowBuilder` with full React Flow canvas (`Canvas.tsx`, `StepNode.tsx`, `StepPalette.tsx`).
- `DatasetView`: commit graph visualization using React Flow or SVG; diff view; merge with policy picker.
- `project_dataset_links` UI in `ProjectSettings`: pin to commit or float on branch.
- RBAC: `require_project_access` enforced on all routers. Member management UI in `ProjectSettings`.
- Audit trail: `GET /projects/{id}/activity` — paginated events feed.
- Soft-delete GC: Celery beat task (or fallback async loop) runs weekly; honors retention windows from `orgs.settings`.
- Ontology editor UI: add/reorder/deprecate classes with warning banner when reordering would change exported `class_id` assignments.
- Workflow templates: seed migration inserts two starter workflows — "video-intake" (extract → auto-label → human-review → commit) and "retrain-on-latest" (export → train).

---

### Phase 3 — Scale + ML Loop (~3–4 weeks)

- **Celery queue executor:** Replace `SyncExecutor` with a Celery-backed dispatcher. GPU-heavy steps (`auto_label`, `train`) go to a `gpu` queue with `concurrency=2` per worker. All other steps go to the `default` queue. Zero step code changes — only the executor changes.
- **Backpressure:** GPU queue concurrency limit. Excess jobs queue in Redis. API returns `run.status = 'pending'` immediately; client polls or streams.
- **`step.evaluate`:** Load `model_version` weights, run inference on an eval commit, compute mAP against `annotation_revisions`, store metrics on `model_versions.metrics`. Register as `step.evaluate`.
- **Model comparison UI:** `Models` page: select two `model_versions`, compare metrics side-by-side, show which commit each trained on.
- **Active learning stub:** `step.select_for_review` — sort `annotation_revisions` by mean `confidence` ascending, return the N least-confident sample IDs for routing into `human_review`. Closes the model → data → model loop.
- **COCO importer:** `importer.coco` registered step: ingests COCO JSON annotations → `samples` rows + `annotation_revisions` (provenance: import) + commit.
- **FiftyOne:** optional read-only view of samples/commits via FiftyOne's Postgres dataset. Advanced power-user feature; not on critical path.
- **Optional MLflow:** if `training_container.icd_config.mlflow_tracking_uri` is non-null: after training, read the MLflow run ID from `metrics.json`, store in `model_versions.mlflow_run_id`, show "View in MLflow →" link on `ModelDetail` page.
- **CDN / MinIO tiering:** configure MinIO bucket lifecycle policies to move cold blobs (exports older than 90 days, superseded model weights) to slower storage. Add optional nginx/CloudFront in front of MinIO for thumbnail caching.
- **`commit_samples` partitioning:** if any project exceeds 5M samples, partition `commit_samples` by `commit_id % 16` using PostgreSQL declarative partitioning.

---

### Phase 4 — Optional DAG Engine (only if parallel step branches are concretely needed)

Swap `SyncExecutor` / Celery dispatcher for Prefect, Dagster, or Temporal. The step contract (`Step` ABC, `StepContext`, artifact in/out) is unchanged. Only the executor changes. Defer until real parallel branch workflows are requested.

---

## 20. Team Task Assignment

| Task | Owner | Hard dependencies |
|---|---|---|
| D1 repo init + docker-compose | Yehuda | none |
| D2 SQLAlchemy + G1 mixin | Yehuda | D1 |
| D3 all migrations 002–010 | Yehuda | D2 |
| D4 MinIO + StorageBackend | Yehuda | D1 |
| D5 FastAPI skeleton + health | Yehuda | D1 |
| D6 auth endpoints | Yehuda | D2, D5 |
| D7 registry | Yehuda | D2, D5 |
| D8 audit helper | Yehuda | D2 |
| D9 React shell | Yehuda | D1 |
| E1 projects + orgs CRUD | Yehuda | D2, D6, D8 |
| E2 data source + upload | Yehuda | D4, E1 |
| **E3 step contract + executor** | **Itai** | D7 (registry contract) + D3 (runs table) |
| **E4 extract_frames** | **Nati/Yahav** | D4 (storage), E3 (step contract) |
| **E5 auto_label** | **Nati/Yahav** | E4 (sample rows to label) |
| E6 commit_dataset | Yehuda | E4, E5 (sample + revision inputs) |
| **E7 export_yolo** | **Nati/Yahav** | E6 (commit row) |
| E8 training_containers API | Yehuda | E1 |
| **E8 train step** | **Nati/Yahav** | E7 (export blob), E8-API (container row) |
| E9 workflows + runs HTTP layer | Yehuda | E1, D7 |
| E9 executor wiring | **Itai** | E3 + Yehuda's E9 HTTP layer |
| E10 sample browser | Yehuda | E4 |
| E11 frontend pages | Yehuda (layout, WorkflowBuilder) + Nati/Yahav (optional: SampleGrid, RunView) | D9, E1 |
| E12 full docker-compose integration | Yehuda | all Phase 1 |

**Parallel tracks after Phase 0 completes:**

- **Itai** starts E3 immediately when D7 (registry interface) and D3 (runs table migration) are done. E3 is independent of all of Yehuda's Phase 1 domain work.
- **Nati/Yahav** start E4 when E3 (`Step` contract defined) and D4 (storage) are done. Their chain E4 → E5 → E7 → E8-step is independent of all Yehuda Phase 1 work except D4 and E3.
- **Yehuda** works E1 → E2 → E6 → E8-API → E10 → E11 → E12.
- All three tracks converge at E9 + E12.

**Critical path:** D1 → D2 → D3 → {D4, D5} → {D6, D7, D8} → E3 → E4 → E5 → E6 → E7 → E8 → E9 → E12.

---

## 21. Glossary

**Artifact** — any versioned reference flowing through a workflow step: a blob hash, a sample set, a commit ID, an export blob hash. The unit of "in → out". Steps consume and produce artifact references, never raw bytes in memory.

**Blob** — bytes stored once, keyed by SHA-256 of the content (`sha256:<64-hex>`). One `blobs` table backs all binary content: frames, thumbnails, model weights, export archives, log files.

**Bucket path** — the MinIO/S3 object key for a blob: `blobs/{hash[7:9]}/{hash[9:]}`.

**CAS** — compare-and-swap. The only mechanism that advances a branch head: `UPDATE refs SET target_commit_id = :new WHERE target_commit_id = :expected`. If 0 rows updated, a concurrent writer advanced first; retry with merge or rebase.

**class_key** — a stable string identifier for a label class, e.g. `"vehicle.car"`. Never an integer. Decoupled from YOLO's positional `class_id`, which is derived at export time from `sort_order`.

**Commit** — an immutable row in the `commits` table: a frozen snapshot of a dataset identified by the set of `(sample_id, annotation_revision_id, split)` triples in `commit_samples`. Never updated after insertion.

**Branch** — a mutable pointer (`refs` row with `is_mutable = true`) to the latest commit on a line of work. Advanced by CAS.

**Tag** — an immutable pointer (`refs` row with `is_mutable = false`) permanently naming one commit. Used for training pins.

**ContentAddress** — the property that a blob's storage key is derived solely from its content. Implies: identical bytes deduplicate automatically; the content at a given key can never change; caching requires no invalidation.

**EntityBase / G1 spine** — the SQLAlchemy mixin that every domain table inherits: `id`, `created_at`, `updated_at`, `created_by`, `deleted_at`, `project_id`.

**Gate** — a step with `is_gate = True` that raises `GateException` to pause a workflow run in the `waiting` state. The `human_review` step is the only gate step in Phase 1+2. The run resumes when `resume_workflow()` is called.

**ICD** — Interface Contract Document. The JSON structure in `training_containers.icd_config` that describes how CVOps communicates with a user-supplied training Docker container: what environment variables map to inputs, what file paths contain outputs.

**Idempotency key** — a SHA-256 hash of `{type_key, config, inputs}` for a step invocation. If a prior step run with the same key and `status = 'succeeded'` exists in the project, the executor reuses its outputs without re-executing.

**Merge policy** — a registered strategy (type_key `merge_policy.*`) that resolves annotation conflicts when two branches are merged. Default: `merge_policy.human_over_model`.

**Ontology** — a versioned set of `label_classes` for a project. Commits record which ontology version they used.

**Perceptual hash (phash)** — a hash computed from the visual content of an image using the pHash algorithm (`imagehash` library). Used to detect near-duplicate frames during extraction. Hamming distance between phashes indicates visual similarity.

**Pin / float** — a `project_dataset_link` that either pins a specific commit (`pinned_commit_id` set) or floats on a branch (`ref_id` set). Pinned = reproducible/immutable. Floating = tracks latest.

**Presigned URL** — a short-lived, single-object signed URL issued by the API allowing a client to read or write one blob directly from MinIO/S3 without API involvement in the data transfer.

**Registry** — the in-memory singleton in `core/registry.py` mapping `type_key → Step implementation`. Backed by the `type_schemas` table for persistence and UI exposure.

**run** — a row in the `runs` table representing one execution of a workflow or one step. Carries `status`, `input_refs`, `output_refs`, `config`, `metrics`, `logs_blob_hash`, `attempt`, `error`.

**Sample** — one image row in the `samples` table, identified by `(project_id, blob_hash)`. Immutable once created. The atom of a dataset.

**Split leakage** — the data quality bug where frames from the same video appear in both the train and val splits, inflating validation metrics. Prevented by the `by_source_group` split strategy.

**StorageBackend** — the `ABC` in `core/storage.py` with methods `save_bytes`, `save_stream`, `get_presigned_get`, `get_presigned_put`, `get_bytes`, `delete_blob`. `MinIOBackend` is the Phase 1 implementation. Swap to S3/GCS by changing `MINIO_ENDPOINT` and credentials — zero code change.

**type_schemas** — the PostgreSQL table and in-memory registry that maps `type_key → json_schema + ui_hints`. Every pluggable thing (step, exporter, backend, merge policy, split strategy, ICD) has a row here. The UI reads these to render config forms.

**Workflow definition** — the `definition` JSONB column on a `workflows` row: `{steps: [...], edges: [[from_id, to_id], ...]}`. A workflow is data, not code. The executor interprets it; the UI renders it as a React Flow canvas.

---

### Critical Files for Implementation
- `C:\_projects\fullstackProjects\Army\workflows\packages\api\src\cvops_api\db\base.py`
- `C:\_projects\fullstackProjects\Army\workflows\packages\api\src\cvops_api\core\registry.py`
- `C:\_projects\fullstackProjects\Army\workflows\packages\api\src\cvops_api\engine\executor.py`
- `C:\_projects\fullstackProjects\Army\workflows\packages\api\src\cvops_api\core\storage.py`
- `C:\_projects\fullstackProjects\Army\workflows\packages\steps\src\cvops_steps\commit_dataset.py`
