# YOLO Workflow Platform — Design Documentation

> **Status:** Implemented (2026-10-10). These are the original design docs; the code has since diverged in places — current state and ordering live in [`../ROADMAP.md`](../ROADMAP.md), superseded docs in [`archive/`](./archive/).
> **Audience:** The team — Yehuda (datasets/versioning/orchestration), Nati & Yahav (extraction + auto-labeling + export), Itai (the synchronous sequence runner).

---

## What we are building

A **dashboard** that lets a user define, run, and manage end-to-end workflows for object-detection data:

```
ingest videos/images → extract frames → auto-label with a trained model →
human review/correction → build & version a dataset → train a model → evaluate → iterate
```

The user composes these stages into **custom workflows**, runs them, and manages the **projects, datasets, dataset versions, and annotations** that flow through them — all from one place.

The hard part is **not** any single stage. Nati & Yahav own the stages; Itai wires a first synchronous sequence. The hard part — Yehuda's part — is the **substrate** underneath: how data is stored, versioned, shared across projects without conflict, served fast, and exposed to the user in a way that stays modular and generic as the system grows. That substrate is what these docs design.

---

## Two ideas hold the whole system together

Almost every decision in these docs derives from two unifying ideas. If you internalize these two, the rest is consequence.

### 1. Immutability + content-addressing + append-only
- **Blobs are content-addressed** (stored by hash). Identical bytes are stored once, are immutable by definition, and are trivially cacheable forever.
- **Dataset versions ("commits") are immutable manifests** of references — never edited in place.
- **Everything writes append-only**; the only mutable things in the system are a handful of small named pointers ("branch heads").

This single stance gives us, for free: deduplication, conflict-free sharing across projects, safe concurrency, perfect reproducibility, and correct-by-construction caching. See [Versioning, Concurrency & Merge](./03-versioning-concurrency-merge.md) and [Storage, Performance & Access](./04-storage-performance-access.md).

### 2. Type + JSON Schema + registry (+ JSONB config)
- Every pluggable thing (a workflow step, a storage backend, a label format, a model runner, an exporter) **registers itself** under a `type` with a **JSON Schema** describing its config.
- Config is stored as **validated JSONB**.

This single stance gives us, also for free: modularity (add a type, change no core code), validation/controls (schema = the gate), and an **auto-generated UI** (forms render from the schema). It is *the* mechanism by which "everything in the flow is opened to the user." See [Modularity & Extensibility](./06-modularity-and-extensibility.md) and [Workflow Engine](./05-workflow-engine.md).

---

## Tooling decisions (summary)

| Concern | Decision | Why (short) |
|---|---|---|
| Annotation / review UI | **CVAT** as a *labeling workstation* (not the source of truth) | Full box editor + model-assisted pre-labeling; push tasks via API, pull results back as annotation revisions. |
| Dataset curation / QA | **FiftyOne** (optional, later) | Query/visualize/eval; can orchestrate CVAT. Skip in MVP if the dashboard covers basic browsing. |
| Metadata source of truth | **PostgreSQL** | Small, queryable, transactional. Holds everything *except* image bytes. |
| Blob storage | **Object storage** (S3 / MinIO / GCS), content-addressed | Throughput-optimized; never queried; served direct to client. |
| Data versioning core | **DB-native** (immutable commit manifests) | A dashboard needs queryable, fine-grained, multi-writer versioning. DVC can't back that. |
| DVC / lakeFS | **Narrow role or skip** | Optionally snapshot a *frozen* version next to training code for reproducibility; lakeFS only if blob-scale versioning becomes a real need. |

Full reasoning in [Principles & Architecture](./01-principles-and-architecture.md).

---

## Read in this order

1. **[Principles & Architecture](./01-principles-and-architecture.md)** — the layers, the cross-cutting principles, build-vs-buy.
2. **[Data Model](./02-data-model.md)** — entities, the generic/global tables, the schema sketch + ER diagram.
3. **[Versioning, Concurrency & Merge](./03-versioning-concurrency-merge.md)** — commits, branches, tags, CAS concurrency, merge, multi-project sharing without conflict.
4. **[Storage, Performance & Access](./04-storage-performance-access.md)** — data/metadata split, presigned access, caching, performance.
5. **[Workflow Engine](./05-workflow-engine.md)** — workflow-as-data, the step contract, the run state machine, human gates.
6. **[Modularity & Extensibility](./06-modularity-and-extensibility.md)** — the registry/plugin patterns and the generic DB patterns reused across the code.
7. **[API & Dashboard UX](./07-api-and-dashboard-ux.md)** — resource API, schema-driven UI, "everything opened to the user."
8. **[Controls, Governance & Security](./08-controls-governance-security.md)** — RBAC, validation, audit/lineage, least-privilege, idempotency, observability.
9. **[Gaps & Considerations](./09-gaps-and-considerations.md)** — the things easy to miss; edge cases, risks, decisions to make. **Read this even if you read nothing else after the data model.**
10. ~~Glossary & Roadmap~~ — archived ([docs/archive/10-glossary-and-roadmap.md](./archive/10-glossary-and-roadmap.md)). Glossary → [MASTER_PLAN §21](./MASTER_PLAN.md#21-glossary); current ordering → [ROADMAP.md](../ROADMAP.md).

---

## How the team's work maps onto this

- **Nati & Yahav** build the *implementations* behind a few **step types** in the registry: `extract_frames`, `auto_label`, `export_yolo`. Each is an artifact-in → artifact-out unit (see [Workflow Engine](./05-workflow-engine.md)).
- **Itai's** "basic synchronous sequence" is the **first executor** of the workflow engine: a sequential runner over the step contract. Because the contract is fixed, his runner can later be swapped for a queue/DAG engine with no change to the steps.
- **Yehuda** owns the substrate every doc here describes: data model, versioning, storage, registry, API, controls.

---

## Operational docs (current behaviour)

- **[15 · Keycloak user management](./15-keycloak-user-management.md)** — the planned auth replacement.
- **[16 · Dataset import](./16-dataset-import.md)** — getting pre-labelled data (YOLO / COCO / raw) into CVOps.
- **[Service ICDs](./services/)**, **[guides](./guides/)**, **[bug write-ups](./bugs/)**.
- Superseded docs live in **[archive/](./archive/)**.
