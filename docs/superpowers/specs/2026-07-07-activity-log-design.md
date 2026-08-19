# Design Spec — Activity Log (FEAT-4)

**Date:** 2026-07-07
**Status:** IN PROGRESS

---

## Goal

Surface the existing `events` table as a read-only, paginated activity feed at `/activity` — a global (org-scoped) page listing every meaningful mutation across the org in reverse-chronological order.

---

## Data Model Gap

The `events` table currently has no `org_id` column. All other resources are filtered by `current_user.org_id`. To keep multi-tenancy consistent, `org_id` must be added and populated at emission time.

**Migration `0003_events_org_id`:**
- Add `org_id UUID nullable` column to `events`
- Add index `ix_events_org_id_created_at` on `(org_id, created_at DESC)` for fast listing

**`Event` ORM model** — add `org_id: Mapped[Optional[uuid.UUID]]`.

**`emit_event` signature** — add `org_id: uuid.UUID | None = None` keyword argument. Inserts it into the row. Existing callers with no `org_id` continue to work; their rows get `NULL` and are invisible to the listing endpoint (acceptable — these are pre-migration historical rows).

---

## Backend

### Updating emit_event callers

Two caller categories:

**Router callers (~12 sites):** have `current_user` available. Pass `org_id=current_user.org_id`.

Files to update: `routers/collections.py`, `routers/samples.py`, `routers/data_sources.py`, `routers/tags.py`, `routers/datasets.py`, `core/annotation_import.py`.

**Engine/coordinator callers (~7 sites in `engine/coordinator.py`):** no `current_user`, but the coordinator already loads the parent `Run` which has `project_id`. Derive `org_id` once at the top of `advance_workflow`:

```python
proj = await session.get(Project, parent.project_id)
org_id = proj.org_id if proj else None
```

Pass `org_id=org_id` to every `emit_event` call within that function.

### New router: `routers/events.py`

**Endpoint:** `GET /api/v1/events`

**Auth:** standard `get_current_user` dependency.

**Query parameters:**
- `entity_type: str | None` — filter by entity type (e.g. `"run"`, `"commit"`, `"data_source"`)
- `action: str | None` — filter by action string (e.g. `"run.started"`, `"created"`)
- `cursor: str | None` — base64-encoded `"{created_at_iso}:{id}"` of the last seen item
- `limit: int = 50` (max 200)

**Org isolation:** `WHERE org_id = current_user.org_id`

**Cursor pattern** (newest-first, composite on `created_at DESC, id DESC`):
```sql
WHERE org_id = :org_id
  AND (created_at, id) < (:cursor_ts, :cursor_id)   -- when cursor present
ORDER BY created_at DESC, id DESC
LIMIT :limit + 1
```
If `limit+1` rows returned, slice to `limit` and encode `"{last.created_at.isoformat()}:{last.id}"` as `next_cursor`.

**Actor name join:** left join `users` on `events.actor_id = users.id` to include `actor_email: str | None` in the response.

**Response schema `EventOut`:**
```python
class EventOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    actor_id: uuid.UUID | None
    actor_type: str
    actor_email: str | None      # joined from users
    entity_type: str
    entity_id: uuid.UUID
    action: str
    payload: dict | None
```

**List response:** `{"items": [...], "next_cursor": str | None}`

**Mount:** added to `main.py` under `/api/v1` prefix (same pattern as `runs`, `datasets`, etc.).

---

## Frontend

### `src/api/events.ts`

TanStack Query `useInfiniteQuery` hook `useEvents(filters)`:
- `queryKey: ['events', filters]`
- `queryFn`: `GET /api/v1/events?entity_type=...&action=...&cursor=...&limit=50`
- `getNextPageParam`: returns `next_cursor` from last page, or `undefined`

Export `EventOut` TypeScript type mirroring the backend schema.

### `src/pages/Activity.tsx`

Layout:
- Page heading "Activity"
- Filter chips row: entity_type options (`All`, `Run`, `Commit`, `Data Source`, `Sample`, `Annotation`) + action search input (free-text)
- Infinite-scroll timeline feed (IntersectionObserver on a sentinel div calls `fetchNextPage`)
- Empty state: "No activity yet"
- Loading skeleton: 5 placeholder rows

Each feed row:
- **Icon** (16×16, colored by entity_type: run→iris, commit→lime, data_source→amber, etc.)
- **Description** — human-readable string built from `entity_type + action`:
  - `run / run.started` → `"Run started"`
  - `commit / created` → `"Dataset committed"`
  - `data_source / created` → `"Data source uploaded"`
  - `annotation_revision / created` → `"Annotations saved"`
  - fallback: `"{entity_type} {action}"`
- **Actor** — `actor_email` (trimmed to username part before `@`) or `actor_type` if no email
- **Time** — relative (`2 minutes ago`) using the existing date formatting in the codebase; full ISO on hover tooltip

No clickable entity links in V1 (entity routing table is out of scope).

### Route + Sidebar

**`App.tsx`:** `<Route path="/activity" element={<Activity />} />`

**`Sidebar.tsx`:** Add `<NavLink to="/activity">Activity</NavLink>` in the global nav after `Ontologies`.

---

## What's Skipped (V1)

- Clickable links from event rows to the entity (commit page, run detail, etc.)
- Date range filter
- Payload detail expansion / raw JSON drawer
- Backfilling `org_id` on pre-migration rows (they stay invisible, which is fine)
- Events emitted from `worker-preprocessing` (it calls internal endpoints, no `current_user` — these are already captured by the coordinator's emit_event calls)
