# CVOps — Service Contracts

Each service has its own ICD (Interface Control Document) in this directory.
An ICD defines what a service needs, what it reads, what it writes, and what it explicitly does NOT do.

**Last updated:** 2026-06-16

---

## Documents

| File | Service | Owner |
|---|---|---|
| [api.md](./api.md) | API (FastAPI) | Yehuda |
| [worker-preprocessing.md](./worker-preprocessing.md) | Preprocessing Worker | Nati / Yahav |
| [worker-cvat.md](./worker-cvat.md) | CVAT Worker | TBD |
| [worker-training.md](./worker-training.md) | Training Worker | Nati / Yahav |
| [frontend.md](./frontend.md) | Frontend (React) | TBD |
| [step-contract.md](./step-contract.md) | Step ABC (shared library) | Itai |
| [redis-streams.md](./redis-streams.md) | Redis Stream message format | Yehuda |
| [data-layer.md](./data-layer.md) | PostgreSQL + MinIO data layer (not a microservice) | Yehuda |
| [mlflow.md](./mlflow.md) | MLflow tracking server | Ben |
| [observability.md](./observability.md) | Logs + infra metrics (Loki/Prometheus/Grafana) — `--profile observability` | TBD |

---

## Shared Concepts

### Job Lifecycle

Every unit of async work is a row in the `runs` table. This is the single source of truth for job state — Redis Streams carry only a wake-up signal.

```
pending   → worker picks it up        → running
running   → step completes            → succeeded
running   → step raises gate          → waiting    (human review pause)
waiting   → gate resolved             → running    (resumes next step)
running   → step raises error         → failed
failed    → user retries              → running    (attempt + 1)
```

### Worker Token

Workers make no API calls — they read/write PostgreSQL directly and advance workflows in-process via `advance_workflow`. `WORKER_TOKEN` is not validated by the API (#71); its only live use is the bearer the API sends to the model deployer. Never use user credentials in workers.

### Presigned URL Pattern

No service proxies bytes. Ever.

```
wants bytes → asks API for presigned URL → API returns URL → goes directly to MinIO
```

The API is the gatekeeper (auth + authorization). MinIO is the byte store. They are never mixed.

---

## Communication Map

```
                      BROWSER
                         │
           ┌─────────────┼──────────────┐
           │ REST/SSE    │              │ presigned URLs
           ▼             │              ▼
         NGINX           │            MinIO
           │             │         (bytes only)
     /api/ │   / │       │              ▲
           ▼   ▼         │              │
         API  Frontend   │    ┌─────────┤
           │             │    │         │
           │ write runs  │    │  write  │
           │ + XADD      │    │  blobs  │
           ▼             │    │         │
         Redis           │    │         │
         Streams         │    │         │
           │             │    │         │
     ┌─────┼─────┐       │    │         │
     ▼     ▼     ▼       │    │         │
  worker worker worker   │    │         │
  prep  cvat  train      │    │         │
     │     │     │       │    │         │
     │     │     └───────┼────┘         │
     │     │             │              │
     └─────┴─────────────┘              │
           │ write to PG                │
           ▼                            │
       PostgreSQL ─────────────────────►│
       (facts, job state)     presigned URL generation

  worker-cvat  ──► CVAT           (push tasks, pull annotations)
  worker-training  ──► Docker daemon  (launch training containers)
```

### Who talks to what

| From | To | How | What |
|---|---|---|---|
| Browser | API | HTTP REST | auth, CRUD, workflow runs |
| Browser | API | SSE | live run events |
| Browser | MinIO | HTTP presigned | upload / download bytes |
| API | PostgreSQL | SQLAlchemy async | all reads and writes |
| API | Redis | redis-py | locks, cache, XADD to streams |
| API | MinIO | boto3 | presigned URL generation only |
| worker-preprocessing | PostgreSQL | asyncpg | job pickup + result write |
| worker-preprocessing | MinIO | boto3 | read source blobs, write outputs |
| worker-preprocessing | Redis | redis-py | XREADGROUP, XACK |
| worker-cvat | PostgreSQL | asyncpg | job pickup + revision write |
| worker-cvat | Redis | redis-py | XREADGROUP, XACK |
| worker-cvat | CVAT | httpx REST | push tasks, pull annotations |
| worker-cvat | API | httpx REST | `POST /runs/{id}/gates/{step}/resolve` |
| worker-cvat | Nuclio / Docker | `nuctl` CLI | deploy `.pt` models as CVAT auto-label functions (`step.deploy_model`, `POST /deploy` on :8001) |
| API | worker-cvat | httpx REST | proxy model deploy/list via `MODEL_DEPLOYER_URL → worker-cvat:8001` |
| worker-training | PostgreSQL | asyncpg | job pickup + model_version write |
| worker-training | MinIO | boto3 | read dataset, write weights |
| worker-training | Redis | redis-py | XREADGROUP, XACK |
| worker-training | Docker daemon | docker-py | container launch + monitor |
