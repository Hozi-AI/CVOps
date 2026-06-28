# Bug Backlog — 2026-06-28

## BUG-1: WorkflowBuilder "Run workflow" fires with no params (FIXED, pending merge)

**Symptom:** `Step 'extract' input resolution: Run param 'source_id' not found`  
**Branch:** `worktree-Feat+workflow-run-params-dialog`

**Root cause:** `WorkflowBuilder.tsx` calls `createRun.mutateAsync({ workflowId })` with no `params`. The extract step's inputs template references `$run.params.source_id`, which resolves to nothing and immediately fails the run before any child step is created.

**Fix:** A `RunParamsDialog` modal now intercepts "Run workflow" clicks, detects `$run.params.*` references in the workflow definition via `extractRunParams()`, and prompts the user to fill them in before firing the run. Committed on the worktree branch — needs to be cherry-picked to main and merged.

---

## BUG-2: `commit_dataset` crashes with `['None', 'None', 'None']` UUID cast error

**Symptom:** `Step 'commit' failed: invalid input for query argument $1: ['None', 'None', 'None'] (invalid UUID 'None')`  
**File:** `packages/steps/src/cvops_steps/commit_dataset.py`

**Root cause — two compounding issues:**

**2a. `_lock_branch` returns the string `"None"` instead of Python `None`**

```python
return str(row[1]), str(row[0])   # str(None) == "None" when target_commit_id IS NULL
```

When a `refs` row has `target_commit_id = NULL`, `str(None)` produces the string `"None"`. Later:

```python
if parent_commit_id is not None:   # True! "None" != None
    parent_rows = session.execute("... WHERE commit_id = CAST(:cid AS uuid)", {"cid": "None"})
```

asyncpg passes `"None"` as a SQL parameter. PostgreSQL may return 0 rows or throw, depending on the driver path.

**Fix:** Return `None` (Python) instead of the string:
```python
return (str(row[1]) if row[1] is not None else None), str(row[0])
```

**2b. No None-filtering on `annotation_revision_ids` input**

```python
# line 42 — str(None) == "None" for any null item
revision_ids: list[str] = [str(r) for r in inputs.get("annotation_revision_ids", [])]
```

`human_review.py` already has the fix for this exact case (lines 71-75 with an explanatory comment). `commit_dataset.py` is missing the same guard.

**Fix:** Mirror the human_review pattern:
```python
revision_ids: list[str] = [
    str(r) for r in inputs.get("annotation_revision_ids") or []
    if r is not None and str(r) not in ("None", "")
]
```

---

## BUG-3: `human_review` step declares no outputs in `stepMeta.ts`

**Symptom:** When a workflow is built in the canvas with `human_review → commit_dataset`, the canvas cannot wire `annotation_revision_ids` from the review step into commit because `human_review` has `outputs: []`. The commit step's saved inputs template omits `annotation_revision_ids` entirely.

**File:** `services/frontend/src/lib/stepMeta.ts`

**Root cause:** The CVAT sync path (`worker-cvat/sync.py`) writes `annotation_revision_ids` to the gate run's `output_refs`, but `stepMeta.ts` doesn't declare this as an output port. So the canvas's `findProvider()` never finds review as a provider for `annotation_revision_ids`, and `buildInputs()` leaves it out of the step definition.

**Fix:** Add `annotation_revision_ids` to `human_review`'s outputs:
```ts
'step.human_review': {
  ...
  outputs: ['annotation_revision_ids'],
  ...
}
```

This allows the canvas to auto-wire `annotation_revision_ids: "$steps.<review_id>.outputs.annotation_revision_ids"` into the downstream commit step.
