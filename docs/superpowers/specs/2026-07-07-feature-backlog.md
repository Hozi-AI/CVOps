# Feature Backlog — 2026-07-07

**Status:** IN PROGRESS

Ideas surfaced from a gap analysis against the core ML loop. Ordered roughly by impact-to-effort ratio.

---

## FEAT-1: Dataset stats per commit

**Status:** TODO

Show class distribution, sample counts per split (train/val/test), and annotation coverage on the commit detail page.

**Why it matters:** First thing you check before kicking off a train. Data is already in Postgres — mostly a query + chart.

**Rough scope:** One SQL query aggregating `commit_samples + annotation_revisions`, one chart component on the commit page.

---

## FEAT-2: Run retry

**Status:** TODO

A "Retry" button on a failed run that re-dispatches the same workflow with the same params.

**Why it matters:** Async runs fail (network blip, CVAT down, etc.). Currently you rebuild the entire run from scratch.

**Rough scope:** One endpoint `POST /runs/{id}/retry`, one button in the run detail UI.

---

## FEAT-3: Model comparison

**Status:** TODO

Side-by-side view of two or more model versions — metrics, commit they trained on, training container.

**Why it matters:** Core question after training: "did this new dataset commit actually improve things?"

**Rough scope:** A compare page/modal that queries multiple `model_versions` rows and renders a diff table.

---

## FEAT-4: Activity / audit log UI

**Status:** TODO

A page (or panel) that surfaces the `events` table — who did what, when, on which resource.

**Why it matters:** The append-only `events` table is already populated. No UI exposes it. Essential for debugging "what happened to this run."

**Rough scope:** Paginated list page with filters by resource type / actor. Pure read, no new backend logic needed.

---

## FEAT-5: Run completion notifications

**Status:** TODO

Push a notification (toast + sidebar badge) when an async run finishes or fails — no more manual polling.

**Why it matters:** Runs take minutes. Users open another tab and forget. A push signal closes the feedback loop.

**Rough scope:** SSE or WebSocket endpoint the frontend subscribes to; backend emits on run status change.

---

## FEAT-6: RBAC

**Status:** TODO

Role-based access within an org: admin, annotator, viewer.

**Why it matters:** Multi-user orgs. Currently all members of an org can do everything.

**Rough scope:** `role` column on `org_members`, middleware check on mutating endpoints, UI gating.

---

## FEAT-7: Auto-label step

**Status:** TODO (stub exists in `cvops_steps`)

Run a model against unlabeled samples to produce draft `annotation_revisions`, then gate on human review.

**Why it matters:** Biggest productivity unlock — seed annotations instead of labeling from scratch.

**Rough scope:** Implement the `auto_label` step stub; needs a model inference endpoint or CVAT model integration.

---

## FEAT-8: Commit diff

**Status:** TODO

Show what changed between two commits: samples added/removed, annotations changed.

**Why it matters:** Dataset versioning is useless without a diff view. "What exactly changed in v3 vs v2?"

**Rough scope:** Query against `commit_samples` for two commit IDs, render added/removed/changed counts with sample previews.
