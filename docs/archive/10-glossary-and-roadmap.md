> **Archived 2026-10-10.** Superseded by [MASTER_PLAN.md §19](../MASTER_PLAN.md#19-phased-build-plan) (build plan) and [ROADMAP.md](../../ROADMAP.md) (current ordering); the glossary lives in [MASTER_PLAN.md §21](../MASTER_PLAN.md#21-glossary). Kept for history; do not treat as current.

# 10 · Glossary & Roadmap

← [Gaps & Considerations](../09-gaps-and-considerations.md) · [Back to README](../README.md)

---

## Glossary

Shared vocabulary so the team means the same thing. (Terms link to where they're defined in depth.)

- **Artifact** — any versioned thing that flows through a workflow: a blob (image/model/export) or a structured reference (sample set, commit). The unit of "in → out" for steps. ([doc 05 §2](../05-workflow-engine.md#2-the-step-contract-p7-artifacts-in--artifacts-out))
- **Blob** — a pile of bytes stored once, keyed by the **content hash** of those bytes. The single content-addressed primitive. ([doc 02 §G2](../02-data-model.md#g2--the-blob-the-one-content-addressed-store))
- **Content-addressing** — keying bytes by their hash, so identical content stores once, is immutable, and is cacheable with no invalidation. ([doc 04](../04-storage-performance-access.md))
- **Sample** — one image (immutable once created) plus its metadata. The atom of a dataset. ([doc 02](../02-data-model.md#samples-images--the-atom))
- **Annotation revision** — an append-only version of the labels for one sample, with provenance (model/human) and review status. Never edited in place. ([doc 02](../02-data-model.md#annotations-append-only-with-provenance))
- **Ontology / class_key** — the versioned set of classes; `class_key` is a *stable string* id, decoupled from YOLO's positional integer `class_id` (which is derived at export). ([doc 02](../02-data-model.md#ontology--classes-a-shared-versioned-resource))
- **Dataset** — a named, evolving collection within a project. ([doc 02](../02-data-model.md))
- **Commit** — an **immutable** snapshot of a dataset: the manifest of `(sample → annotation_revision, split)` + ontology version + parent(s). ([doc 03](../03-versioning-concurrency-merge.md#1-the-mental-model))
- **Branch** — a **mutable pointer** to the latest commit on a line of work; advanced by compare-and-swap. ([doc 03 §3](../03-versioning-concurrency-merge.md#3-concurrency-optimistic-lock-free-conflict-explicit))
- **Tag** — an **immutable pointer** naming one commit (e.g. `v1.0`); what training pins to. ([doc 03](../03-versioning-concurrency-merge.md#1-the-mental-model))
- **Pin / float** — a project *pins* a commit (reproducible, frozen) or *floats* on a branch (tracks latest). The basis of conflict-free sharing. ([doc 03](../03-versioning-concurrency-merge.md#multiple-projects-no-conflict))
- **Presigned URL** — a short-lived, single-object, signed URL letting a client read/write a blob **directly** from object storage, bypassing the app server. ([doc 04 §2](../04-storage-performance-access.md#2-serving-large-data-without-touching-the-app-server))
- **Step** — a registered workflow operation obeying the artifact-in → artifact-out contract. ([doc 05 §2](../05-workflow-engine.md#2-the-step-contract-p7-artifacts-in--artifacts-out))
- **Gate** — a step that pauses a run for external/human input (e.g. `human_review`); puts the run in `waiting`. ([doc 05 §3](../05-workflow-engine.md#run-state-machine))
- **Run / step run** — one execution of a workflow / of a step; rows in the generic `runs` table with status, refs, logs, metrics. ([doc 02 §G4](../02-data-model.md#g4--the-generic-run--the-generic-event-log))
- **Registry** — the catalog mapping a `type_key` → (JSON Schema, implementation) for every pluggable thing. ([doc 06 §1](../06-modularity-and-extensibility.md#1-the-registry-pattern-pluggable-behavior))
- **type_schemas / typed config** — config stored as JSONB validated against a registered JSON Schema; the one pattern behind extensibility, validation, and auto-generated UI. ([doc 02 §G3](../02-data-model.md#g3--typed-config--json-schema-type_schemas))
- **Provenance / lineage** — where an artifact came from (provenance) and the full chain back to source (lineage). ([doc 08 §3](../08-controls-governance-security.md#audit--lineage))
- **CAS** — compare-and-swap; the lock-free mechanism that advances a branch head safely under concurrency. ([doc 03 §3](../03-versioning-concurrency-merge.md#3-concurrency-optimistic-lock-free-conflict-explicit))

---

## Roadmap (phased build plan)

A pragmatic order that delivers value early without painting us into a corner. Each phase is shippable.

### Phase 0 — Foundations
- The generic primitives: base entity (G1), `blobs` + content-addressed save (G2), `runs`/`events` (G4). ([doc 02 §1](../02-data-model.md#1-the-generic-building-blocks-reused-across-the-whole-codebase))
- Postgres + object storage (MinIO/S3) wired; presigned upload/download working. ([doc 04](../04-storage-performance-access.md))
- Projects, samples, ontology with stable `class_key`.
- **Outcome:** can ingest images and serve them fast, safely, deduplicated.

### Phase 1 — MVP: a working single-pass pipeline
- Step contract + registry + JSON-Schema config validation (G3, [doc 06](../06-modularity-and-extensibility.md)).
- Steps: `extract_frames`, `auto_label`, `export_yolo` (Nati & Yahav).
- **Itai's synchronous executor** running a fixed sequence. ([doc 05 §4](../05-workflow-engine.md#4-executors-grow-without-changing-the-contract))
- Datasets + commits + branches/tags + CAS; `commit_dataset` step; `by_source_group` split. ([doc 03](../03-versioning-concurrency-merge.md))
- Minimal dashboard: data sources, sample browser (thumbnails), run a workflow, view a run.
- **Outcome:** video → frames → auto-labels → versioned dataset → YOLO export → train, end to end, reproducibly. The demo.

### Phase 2 — Humans in the loop & real versioning UX
- CVAT integration; `human_review` gate; ingest reviewed revisions with provenance. ([doc 08 §5](../08-controls-governance-security.md#cvat-sync))
- Commit graph view, diffs, **merge** with a policy; multi-project pin/float links. ([doc 03](../03-versioning-concurrency-merge.md))
- Schema-driven **workflow builder** UI; workflow versioning + templates. ([doc 07 §2](../07-api-and-dashboard-ux.md#2-the-self-describing-ui-why-p6-is-nearly-free))
- RBAC, audit trail surfaced, soft-delete + GC. ([doc 08](../08-controls-governance-security.md))
- **Outcome:** the actual product — users compose and run custom workflows, review labels, and manage versioned datasets across projects.

### Phase 3 — Scale, ops, and the ML loop
- Queue executor (Redis + GPU workers); backpressure/limits. ([doc 05 §4](../05-workflow-engine.md#4-executors-grow-without-changing-the-contract))
- CDN + thumbnail tiering; lifecycle policies; partitioning if needed. ([doc 04](../04-storage-performance-access.md))
- `evaluate` step + model comparison; optional FiftyOne curation; optional active-learning `select_for_review`. ([doc 09 §B](../09-gaps-and-considerations.md#b-the-ml-loop-often-forgotten-in-data-plumbing-designs))
- Importers (COCO/VOC/YOLO); experiment-tracking integration if chosen. ([doc 09 §C](../09-gaps-and-considerations.md#import-of-existing-datasets))
- **Outcome:** handles large data, parallel runs, and the full iterate-on-the-model loop.

### Phase 4 — Optional DAG engine
- Adopt Prefect/Dagster/Temporal *only if* real DAG parallelism, scheduling, and backfills are needed — with **no change to the step contract**. ([doc 05 §4](../05-workflow-engine.md#4-executors-grow-without-changing-the-contract))

---

## The shortest possible summary

Build on two ideas: **(1)** immutable, content-addressed, append-only storage with small mutable pointers (gives versioning, dedup, conflict-free sharing, free caching, reproducibility), and **(2)** `type` + JSON Schema + registry for everything pluggable (gives modularity, validation, and a self-describing UI). Keep facts in Postgres and bytes in object storage; serve bytes direct via presigned URLs; expose every step, run, and version to the user. Everything else in these docs is a consequence of those choices.
