# 09 · Gaps & Considerations

← [Controls, Governance & Security](./08-controls-governance-security.md) · Next → [Glossary & Roadmap](./10-glossary-and-roadmap.md)

This is the "things you might have missed" document. These are real, mostly CV/YOLO- and video-specific sharp edges and decisions. Each is stated as *the problem*, *why it bites*, and *the recommended stance*. **This is the highest-value doc to review before committing to the build** — several of these change the data model if discovered late.

---

## A. Data & dataset correctness

### Split leakage — the classic, expensive mistake {#split-leakage}
**Problem.** If frames from the *same video* land in both train and val, your validation metrics are inflated — the model is being tested on near-copies of what it trained on.
**Why it bites.** Video frames are highly correlated (consecutive frames are almost identical). Naive random splitting almost guarantees leakage.
**Stance.** Splits must be **group-aware**: assign at the *source/video* (or scene/track) level, never per-frame. Make `by_source_group` a first-class `SplitStrategy` ([doc 06](./06-modularity-and-extensibility.md)) and the default for video-derived data. Store the assignment in `commit_sample.split` so it's reproducible. This is the single most important item in this doc.

### Near-duplicate frames {#near-duplicates}
**Problem.** Extracting at a fixed interval yields many almost-identical frames, bloating the dataset and biasing it toward static scenes.
**Stance.** Compute a **perceptual hash** per sample ([doc 02 samples](./02-data-model.md#samples-images--the-atom)); offer de-duplication (drop frames below a similarity threshold) as an extraction option, and surface near-dupes in the sample browser. Cheap to add now, painful to retrofit.

### Object tracking across frames (huge labeling-efficiency win)
**Problem.** Labeling each frame independently is wasteful for video; the same car appears in 50 consecutive frames.
**Stance.** Reserve a `track_id` on each annotation object (already in the canonical payload, [doc 02 annotations](./02-data-model.md#annotations-append-only-with-provenance)). It lets CVAT's interpolation/tracking propagate boxes across frames and lets you sample diverse frames per track. Even if you don't use it day one, having the field avoids a migration later.

### Annotation types beyond boxes {#annotation-types-beyond-boxes}
**Problem.** YOLO does detection (boxes) today, but segmentation, oriented boxes, keypoints, and classification often follow.
**Stance.** The canonical `geometry` in the payload is **general** (a typed shape), not a bare bbox. Bake this generality in now so adding masks/polygons later is data, not a schema migration. Keep YOLO-bbox as one *export* projection of the general geometry.

### Ontology evolution (rename / merge / split / deprecate classes) {#ontology-evolution}
**Problem.** Class sets change: "truck" splits into "truck"/"van"; two classes merge; a class is renamed or retired. If labels are tied to positional integer ids, this silently corrupts everything.
**Why our model helps.** Stored labels use a **stable `class_key`**, and integer `class_id` is derived only at export ([doc 02 ontology](./02-data-model.md#ontology--classes-a-shared-versioned-resource)). Renames are a display change; reorders only affect `data.yaml`.
**Still needs a decision.** Merges/splits require an explicit, audited **remap operation** that produces *new* annotation revisions (never mutating old ones), and **cross-ontology dataset merges** need a class-mapping step first ([doc 03 merge](./03-versioning-concurrency-merge.md#4-merging--integrating-data)). Decide the remap UX and where the mapping lives (a versioned mapping document).

### Determinism of auto-labeling {#determinism-of-auto-labeling}
**Problem.** "Re-run auto-label" must be reproducible, or provenance is meaningless.
**Stance.** Pin the **exact model version + preprocessing + confidence threshold** in each revision's provenance; the idempotency key includes them ([doc 05 §5](./05-workflow-engine.md#5-idempotency-determinism-resumability)). Same inputs → same labels → safe to cache and to trust the lineage.

---

## B. The ML loop (often forgotten in "data plumbing" designs)

### Active learning / "what to label next"
**Problem.** Labeling budget is finite; labeling random frames is inefficient.
**Stance.** Use the auto-label confidences already stored to rank frames by uncertainty and feed the most informative ones into review first. Model it as an optional step (`select_for_review`) — it closes the model → data → model loop and dramatically improves data efficiency. Not MVP, but design the data (confidences on objects) so it's possible.

### Experiment tracking & model comparison
**Problem.** Many training runs with different data/hyperparams; you need to compare them honestly.
**Stance.** `model_version` already pins `trained_on_commit` + hyperparams + metrics ([doc 02 models](./02-data-model.md#models--training)). Compare models on the **same eval commit** to make comparisons fair. Optionally integrate W&B/MLflow as the rich tracking UI rather than rebuilding one.

### Evaluation as a first-class step
**Problem.** Training without a standard eval makes "is this better?" unanswerable.
**Stance.** Make `evaluate` (model_version + held-out commit → metrics) a registered step from early on; store metrics on `model_version`. Drives the "model performance across versions" observability ([doc 08 §6](./08-controls-governance-security.md#observability)).

### Inter-annotator agreement / QA
**Problem.** Human labels vary; you may want consensus or spot-checks for quality.
**Stance.** The append-only revision model with provenance supports multiple annotators and a `review_status` workflow. A consensus/gold-set QA layer can be added later as a review policy; design the `review_status` field with room for `needs_second_review`.

---

## C. Scale, cost, and operations

### Very large datasets {#scale--very-large-datasets}
**Problem.** Millions of samples strain naive queries and a single `commit_sample` table.
**Stance.** Cursor pagination everywhere ([doc 04 §5](./04-storage-performance-access.md#5-performance-checklist)); index the hot paths ([doc 02](./02-data-model.md#index-priorities-the-hot-paths)); partition `samples`/`commit_samples` by project (or time) when needed; consider a search index (e.g. for metadata filtering) and the **packed-manifest-blob** option for commits at extreme scale ([doc 02 commit_sample](./02-data-model.md#datasets--versioning-full-detail-in-doc-03)). Don't build these on day one; leave the seams.

### Cost & storage lifecycle {#cost--storage-lifecycle}
**Problem.** Object storage egress and capacity cost real money; thumbnails and old exports accumulate.
**Stance.** Bucket **lifecycle policies** move cold blobs (old exports, superseded weights) to cheaper tiers; GC reclaims unreferenced blobs ([doc 03 §GC](./03-versioning-concurrency-merge.md#garbage-collection)); the CDN cuts egress for repeated reads ([doc 04 §3](./04-storage-performance-access.md#3-caching--and-why-content-addressing-makes-it-free)). Budget for thumbnail storage explicitly.

### Schema migrations vs. immutable commits
**Problem.** The DB schema will evolve, but commits are immutable promises.
**Stance.** Version the **payload/manifest format**; commits record which format version they used, and readers handle old formats. Treat annotation `payload` and commit manifests as **versioned document formats**, not just rows, so old commits stay interpretable forever.

### Frame extraction reproducibility & robustness
**Problem.** Codecs, variable frame rates, EXIF orientation, and corrupt videos make "extract frames" non-trivial and non-deterministic if careless.
**Stance.** Pin extraction parameters (and decoder behavior) so the same video + same config yields the same frames/hashes; normalize orientation; handle corrupt/partial inputs with explicit run failures rather than silent gaps. This is Nati & Yahav's domain, but the *determinism contract* is what the versioning relies on.

### Import of existing datasets
**Problem.** Teams usually have pre-existing labeled data (COCO/VOC/YOLO).
**Stance.** Importers are the mirror of exporters — register `importer.coco`, `importer.yolo` ([doc 06](./06-modularity-and-extensibility.md)); ingest produces samples + annotation revisions (provenance: import) and a commit. Designing import early validates that the canonical model is truly format-agnostic.

---

## D. Product & process

### Multi-tenancy depth
**Decision.** How much org/team/project isolation is needed (shared ontologies across projects? cross-team dataset sharing?). The `org → project` hierarchy supports it; decide the sharing rules explicitly so RBAC ([doc 08 §1](./08-controls-governance-security.md#rbac--permissions)) is designed for them rather than bolted on.

### Data privacy / PII {#data-privacy--pii}
**Decision.** If frames contain faces or plates, you may need blurring, restricted access, or consent tracking depending on jurisdiction and use. Decide per domain; the access controls in [doc 08 §4](./08-controls-governance-security.md#least-privilege) and an optional anonymization step support it.

### Testing strategy for a generic pipeline
**Stance.** The registry/contract design is highly testable: test each `Step`/`Exporter` against its schema and a fixture; test the engine with mock steps; property-test the versioning invariants (immutability, CAS, merge-union correctness). Build a small fixture dataset early.

### Workflow templates & sharing
**Nice-to-have.** Since workflows are data ([doc 05 §1](./05-workflow-engine.md#1-workflows-are-data-not-code)), ship a few **starter templates** ("video intake + review", "retrain on latest") so users aren't staring at a blank canvas. Cheap, big UX win.

---

## F. Known infra tech debt

### CVAT submodule — config files should be vendored {#cvat-submodule-vendoring}
**Problem.** `docker-compose.override.yml` bind-mounts three config files from the `services/cvat` git submodule into CVAT containers (`grafana_conf.yml`, `vector.toml`, `grafana/dashboards/*.json`). The submodule points to a specific CVAT commit that git cannot fetch directly (it isn't a branch/tag tip), so `git submodule update --init services/cvat` fails on a fresh checkout. As a workaround those files were downloaded manually from GitHub and committed into `services/cvat/components/analytics/`.

**Why it bites.** Any developer setting up the repo for the first time hits a broken `tilt up -- --cvat` with no obvious fix. The submodule also drags in 100k+ files of CVAT source that are never used — only those 3 config paths matter.

**Recommended fix.** Remove the `services/cvat` submodule entirely. Copy the 3 config paths into `manifests/cvat-config/` (or inline them directly in `docker-compose.override.yml` via configs/volumes). Update the bind-mount paths. Pin the CVAT image tags in the compose file instead of relying on submodule pointer for version coupling. This makes `tilt up -- --cvat` work on a plain `git clone` with no submodule step.

---

## E. Explicit decisions to make before building

A short list to resolve as a team — most are design forks the docs are built to absorb either way:

1. **Hash function & blob key scheme** (sha256 vs blake3; key layout in the bucket).
2. **Commit membership storage** — `commit_sample` table vs packed manifest blob (start with the table).
3. **Default `SplitStrategy`** — confirm `by_source_group` as default for video (strongly recommended; see §split-leakage).
4. **Conflict/merge default policy** — `human_over_model` vs manual (see [doc 03 §4](./03-versioning-concurrency-merge.md#4-merging--integrating-data)).
5. **CVAT completion signal** — webhook vs polling ([doc 08 §5](./08-controls-governance-security.md#cvat-sync)).
6. **On-prem (MinIO) vs cloud (S3/GCS)** — affects egress cost and CDN choice.
7. **Annotation `geometry` schema** — lock a general shape now to avoid migrations (see §annotation-types-beyond-boxes).
8. **Retention windows** for soft delete + GC ([doc 08 §8](./08-controls-governance-security.md#retention)).
9. **Adopt W&B/MLflow** for experiment tracking, or keep it in-house?
10. **MVP scope cut line** — see the phased plan in [doc 10](./10-glossary-and-roadmap.md).
