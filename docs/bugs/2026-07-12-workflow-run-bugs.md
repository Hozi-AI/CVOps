# Workflow Run Bugs — 2026-07-12

Bugs found and fixed (or diagnosed) while driving the ingest workflow
`extract_frames → human_review → commit_dataset` end-to-end for the first time.

---

## Fixed

### 1. `extract_frames` config validation rejects empty config
**File:** `packages/steps/src/cvops_steps/schemas/extract_frames.json`

`interval_seconds` was listed in `required` even though it had `default: 2.0`.
JSON Schema validation ran before the step could apply defaults, so running the
step with no config object at all failed with a schema error.

**Fix:** removed `interval_seconds` from `required`; also changed the Python
step to `config.get("interval_seconds", 2.0)` instead of `config["interval_seconds"]`.

---

### 2. Workflow builder: dragging edges between nodes did nothing
**File:** `services/frontend/src/pages/WorkflowBuilder.tsx`

All `Handle` elements in `StepNode` were `type="source"`. ReactFlow's default
`ConnectionMode.Strict` only lets edges land on `type="target"` handles, so
every drag attempt was silently dropped.

**Fix:** added `connectionMode={ConnectionMode.Loose}` to the `<ReactFlow>`
component, which allows source-to-source connections.

---

### 3. Run dialog asked for `annotation_revision_ids` and `sample_ids` as manual params
**File:** `services/frontend/src/lib/stepMeta.ts`

`human_review` and `commit_dataset` both listed `annotation_revision_ids` in
`runParamInputs`. When no upstream step in the graph provided the value,
the frontend serialized it as `$run.params.annotation_revision_ids` in the
saved workflow definition — and the Run dialog then demanded the user type it
in manually.

`annotation_revision_ids` is optional (the step handles an empty list) and
should come from an upstream step or default to nothing; it is never a
user-supplied run param.

**Fix:** removed `annotation_revision_ids` from `runParamInputs` on both
`human_review` and `commit_dataset`.

---

### 4. Ref resolver regex failed to match step IDs containing dots
**File:** `services/api/src/cvops_api/engine/ref_resolver.py`

The ref format is `$steps.<step_id>.outputs.<name>`. Step IDs are generated as
`${typeKey}-${Date.now()}` in the frontend, and type keys contain dots
(e.g. `step.extract_frames`), producing IDs like `step.extract_frames-1783852552587`.

The resolver regex was `^\$steps\.([^.]+)\.outputs\.(.+)$`. The `[^.]+` group
stopped at the first dot inside the step ID, so the full regex never matched
and the raw ref string was passed through silently to the step — which then
blew up with a Postgres UUID-parsing error when it tried to iterate the string
as a list.

**Fix (regex):** changed to `^\$steps\.(.+)\.outputs\.([^.]+)$` — greedy
`.+` on the step ID, `[^.]+` on the output name (output names have no dots).
Python backtracks to the last `.outputs.` occurrence.

**Fix (fail-fast):** added a guard so any `$`-prefixed string that still
doesn't match either `$steps.*` or `$run.params.*` raises `ResolutionError`
immediately, failing the run at creation time with a clear message instead of
silently passing the string through to the step.

**Fix (prevention):** changed node ID generation to replace dots with
underscores (`typeKey.replace(/\./g, '_')`) so new workflows produce IDs like
`step_extract_frames-1783852552587` — unambiguous with the old regex.
Existing saved workflows with dotted IDs still work via the greedy `.+` regex.

---

### 5. Worker-preprocessing didn't watch `services/api/src`
Not a code bug, but caused several confusing "fix is in but still broken"
cycles. The Tiltfile's `worker-preprocessing` resource only watches
`services/worker-preprocessing/src` and `packages/steps/src`, not
`services/api/src`. The coordinator (`advance_workflow`, `process_step`) and
`ref_resolver` all live in `services/api/src` — shared between the API and the
worker via editable install — but only the API hot-reloads when they change.
The worker must be manually restarted in Tilt after touching those files.

**Suggested fix:** add `'services/api/src/cvops_api/engine'` to the
`worker-preprocessing` deps in the Tiltfile.

---

## Open Bug

### 6. `commit_dataset` fails with "0 annotation revisions provided" when human_review hits idempotency across different source videos

**Symptom:** `commit_dataset` fails with:
> no sample has an annotation revision among 0 provided; nothing to commit

**Root cause:** `human_review`'s idempotency key is derived from its config and
resolved inputs (sample IDs). When the same source video is re-ingested the
sample IDs are identical (content-addressed blobs → same dedup), so the worker
reuses the prior succeeded `human_review` output, including its
`annotation_revision_ids`. But if a *different* source video is used that
produces the same sample count and similar frames, or if the previous annotation
revision was deleted or is from a different ontology version, the reused
revision ID won't match any current sample — and `commit_dataset` gets 0
matching revisions.

More broadly: annotation revision IDs are not safe idempotency outputs. They
represent a specific labeling event tied to specific samples at a specific point
in time. Reusing them across runs that may have different samples is wrong.

**Possible fixes:**
- Make `human_review` a non-idempotent step (override `idempotency_key` to
  always return a unique value, or include a run-specific nonce).
- Or scope idempotency to `(sample_ids, ontology_id, labeling_config)` and
  verify the reused revision IDs still exist and still belong to those exact
  samples before short-circuiting.
- Alternatively, the gate-resolve endpoint already queries `labeling_jobs` for
  the current run's annotation_revision_ids; ensuring human_review ALWAYS goes
  through the gate (never hits idempotency) would naturally fix this.
