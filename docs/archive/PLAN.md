> **Archived 2026-10-10.** Superseded by [docs/frontend-design-plan.md](../frontend-design-plan.md). Kept for history; do not treat as current.

# CVOps Frontend Plan

Living document. Step 1 (design tokens + dark mode) is done; everything below is queued.
Source: the `ui-ux-pro-max` skill applied to a technical SaaS dashboard (Linear / Vercel /
W&B / MLflow product family).

---

## Status

| Step | Status |
|---|---|
| 1. Design tokens + dark mode | **Done** |
| 2. Component primitives | Pending |
| 3. Layout shell | Pending |
| 4. Project Dashboard | Pending |
| 5. Files (Data Source detail) | Pending |
| 6. Workflow canvas overhaul | Pending |
| 7. Run View (live DAG + gates + logs) | Pending |
| 8. Data Mapping editor | Pending |
| 9. Graphs across detail pages | Pending |

---

## 1. Design tokens + dark mode  *(done)*

Changes landed:

- `services/frontend/tailwind.config.ts` — full cobalt 50–900 scale, `chart-1..8` Okabe-Ito
  derived categorical palette, `focus` ring token, tinted `shadow-*` tokens that switch on
  theme, `info` recolored from `#0EA5E9` to `#6366F1` to stop colliding with the brand `sky`.
- `services/frontend/src/index.css` — semantic CSS variables on `:root` (light) and
  `[data-theme='dark']` (dark): `--surface-1/2/3`, `--text-primary/secondary/muted/inverse`,
  `--border`, `--border-strong`, `--focus`, `--shadow-color`. Body uses `var(--surface-1)` /
  `var(--text-primary)`. `*:focus-visible` ring is global. `prefers-reduced-motion` honored.
- `services/frontend/src/lib/theme.tsx` — `ThemeProvider`, `useTheme()`, cycles
  light → dark → system, persists to `localStorage`, listens to `prefers-color-scheme` when
  in `system` mode.
- `services/frontend/index.html` — boot script sets `data-theme` *before* React mounts to
  prevent the flash-of-wrong-theme.
- `services/frontend/src/main.tsx` — wraps the tree in `<ThemeProvider>`.
- `services/frontend/src/components/layout/Header.tsx` — sun/moon/monitor toggle, uses the
  semantic tokens (`bg-surface-2 text-text-primary border-border`).

### Palette audit findings that motivated the work

| Issue | Resolution |
|---|---|
| `mist` (#94A3B8) on white = 3.45:1 — fails AA for body text | Add `text-muted` semantic token → `#64748B` (4.78:1). Reserve `mist` for ≥18px. |
| `info` (#0EA5E9 sky-500) collided with brand `sky` (#22D3EE) | Recolored `info` → indigo `#6366F1`. |
| No tonal scale for primary | Added `cobalt-50..900`. |
| No dark mode anywhere | Semantic CSS variables; one `data-theme` flip. |
| No data-viz categorical palette | Added `chart-1..8`, colorblind-safer. |
| No focus token | Added `--focus`; global `*:focus-visible` ring. |
| No elevation scale | Tinted `shadow-*` via `--shadow-color`; auto-darkens in dark mode. |

### Token cheat-sheet for new components

```
bg-surface-1            // page background
bg-surface-2            // cards, panels
bg-surface-3            // modals, popovers
bg-cobalt               // primary CTAs (white text)

text-text-primary       // body text
text-text-secondary     // labels, captions
text-text-muted         // hints, metadata
text-text-onAccent      // text on cobalt/success/error fills

border / border-border          // default border
border-border-strong            // emphasised border

focus:ring-2 focus:ring-focus   // focus ring (or just rely on :focus-visible default)

shadow-sm / shadow-md / shadow-lg    // already theme-aware
```

---

## 2. Component primitives  *(next)*

Build these once, reuse everywhere. Order matters — later items depend on earlier ones.

| Primitive | Notes |
|---|---|
| `<Button>` | `variant`: primary / secondary / ghost / danger. `size`: sm / md / lg. `loading` shows spinner and disables. Min 44×44 tap target. |
| `<IconButton>` | Square variant of Button for icon-only triggers. Requires `aria-label`. |
| `<Card>` + `<CardHeader/Body/Footer>` | `bg-surface-2 border shadow-sm`. |
| `<Sheet>` | Right-side slide-in panel (Radix Dialog primitive, styled as a sheet). For "inspect a row" without route change. Esc closes. Focus trap. |
| `<Dialog>` | Centered modal. Always confirm destructive actions. |
| `<Toast>` | `sonner` on top of Radix Toast. `aria-live="polite"`. Auto-dismiss 3–5s. |
| `<EmptyState>` | Icon + title + hint + primary action. Required on every list before data. |
| `<StatusBadge>` | `pending / running / waiting / success / failed`. Pairs color with icon (not color-only). |
| `<KeyValueGrid>` | 2-column metadata layout for detail pages. |
| `<TabBar>` (page) and `<Pills>` (filter chips) | Radix Tabs. Indicator follows active tab with shared-element transition. |
| `<DataTable>` | Sortable headers (`aria-sort`), cursor-paginated, keyboard-navigable rows, sticky header, row actions menu, virtualized when >50 rows. |
| `<Skeleton>` | Shape-matched per consumer (card / table row / chart). Replaces spinners for >300ms loads. |
| `<KbdShortcut>` | Visual chip for keyboard hints. |
| `<CommandPalette>` (⌘K) | `cmdk`. Universal navigation + actions. |

**Tech added in this step** (see §10 for full diff):

```
@radix-ui/react-dialog       @radix-ui/react-dropdown-menu
@radix-ui/react-tabs         @radix-ui/react-toast
@radix-ui/react-tooltip      @radix-ui/react-visually-hidden
lucide-react                 cmdk
sonner                       @tanstack/react-virtual
date-fns
```

Replace the inline-SVG icons in `Header.tsx` with `lucide-react` as part of this step.

---

## 3. Layout shell

```
┌─────────┬───────────────────────────────────────────────────┐
│         │  Top bar: breadcrumb · search · ⌘K · user         │
│ Sidebar ├───────────────────────────────────────────────────┤
│ 240px   │  Page header: title · tabs · primary CTA          │
│         ├───────────────────────────────────────────────────┤
│         │  Content (max-w-7xl center)                       │
└─────────┴───────────────────────────────────────────────────┘
```

- Sidebar collapses to a **56px icon rail** at `<1280px`, drawer at `<768px`.
- **Breadcrumb** is canonical for back-nav. Navigating back never resets scroll or
  filter state.
- **⌘K** is the only "search" affordance — jump-to-resource, run action, switch project.
- **Page tabs** under header for resource sub-views (e.g. Dataset → `Overview · Commits ·
  Mapping · Lineage`).

Files to touch: `src/components/layout/{Layout,Sidebar,Header,Breadcrumb,CommandPalette}.tsx`.

---

## 4. Project Dashboard  (`/projects/:id`)

Currently empty (`Project.tsx`). Becomes the at-a-glance home after project selection.

```
┌──────────────────────────┬─────────────────────────┐
│  Active runs (live SSE)  │  Recent commits         │
│  – 3 cards w/ progress   │  – timeline w/ icons    │
├──────────────────────────┼─────────────────────────┤
│  Data growth (sparkline) │  Model leaderboard      │
│  samples/day · 30d       │  top 5 by primary metric│
├──────────────────────────┴─────────────────────────┤
│  Pending gates  (action required)                  │
│  – approvable inline                               │
└────────────────────────────────────────────────────┘
```

- 12-col grid; 6+6 desktop, 12 mobile (`content-priority`).
- **Active runs** subscribe to `/runs/:id/events` SSE — the proxy is already in place
  in `vite.config.ts`.
- **Pending gates** is the human-in-the-loop CTA. One primary CTA per row
  (`primary-action`); destructive (Reject) confirms via Dialog.

---

## 5. Files (Data Source detail)  (`/projects/:id/data-sources/:dsId`)

A data source has thousands of underlying blob files. New route + tabs.

```
DS header: name · type · status · ⋮
Tabs: Overview · Files · Extraction
Filter bar: type · ingested-at · size
Virtualized file grid OR list toggle
```

- **Grid**: 4–8 thumbnail columns; `loading="lazy"` `decoding="async"`. Presigned GET URLs
  from the API. Reserve aspect ratio to prevent CLS.
- **List**: `<DataTable>` with hash, size, ingested-at; thumb preview on row hover.
- Virtualization mandatory (`virtualize-lists`, >50 items). `@tanstack/react-virtual`.
- Cursor pagination is already API-side — wire into `useInfiniteQuery`.
- Click a file → `<Sheet>` opens with metadata + full preview. URL stays on parent grid
  so deep links survive.

---

## 6. Workflow canvas overhaul  (`/projects/:id/workflows/:wfId`)

Biggest single piece. Stay on `@xyflow/react`. Goal: feels like Linear, not Photoshop.

```
┌──────────────────────────────────────┬──────────────┐
│  Canvas (zoom 0.4–2.0, pan, minimap) │  Inspector   │
│                                      │              │
│  ╭ step.extract_frames ──╮            │  Selected:   │
│  │ inputs:               ●─→─╮         │  extract_    │
│  │   data_source         │   ↓         │  frames      │
│  │ outputs: samples[]    │             │              │
│  ╰───────────────────────╯             │  config: …   │
│                                        │              │
├──────────────────────────────────────┤  [Validate]  │
│  Step palette (drag) · zoom · fit · ⌘S│  [Run from…] │
└──────────────────────────────────────┴──────────────┘
```

Concrete decisions:

1. **Custom node per `type_key`** registered via the step registry. Fetch step metadata from
   `/registry` at mount and render nodes generically from each step's `config_schema`
   (JSON Schema). Zero code changes when a new step is added.
2. **Inspector form** uses `@rjsf/core` (already installed) on the `config_schema`, themed
   with Tailwind tokens.
3. **Connection validation at draw time**: dragging from an output port lights only valid
   input ports (type-checked via schema). `inline-validation`.
4. **Gate steps** get amber border + small "human" icon — pair color with shape per
   `pattern-texture` (don't rely on color alone).
5. **Run-from-step** menu on each node: "Run from here" / "Run this only" / "Skip on next
   run".
6. **Live overlay during run**: nodes animate edges from upstream → downstream as steps
   complete. One duration token (220ms ease-out) per `motion-consistency`. Reduced-motion
   swap: static green outline.
7. **Minimap** bottom-right; **fit-to-view** `F`; **undo/redo** `⌘Z / ⌘⇧Z` via Zustand
   temporal middleware (Zustand already in use).

---

## 7. Run View  (`/projects/:id/runs/:runId`)

```
┌──────────────────┬────────────────────────────────┐
│  Run header      │  Tabs: DAG · Logs · Outputs    │
├──────────────────┼────────────────────────────────┤
│  Steps timeline  │                                │
│  (gantt)         │   Selected step content        │
│                  │   – logs (virtualized)         │
│                  │   – preview of outputs (blobs) │
│                  │   – gate UI if step is paused  │
└──────────────────┴────────────────────────────────┘
```

- SSE from `/runs/:id/events` updates step statuses live.
- **Gate UI**: inline form, Approve (primary) / Reject (danger) / Defer (ghost). Reject
  confirms via Dialog.
- Logs use `react-window` virtualization. "Jump to error" button finds first ERROR.

---

## 8. Data Mapping editor  (`/projects/:id/datasets/:dsId/mapping`)

Transform raw samples → labeled dataset records.

```
┌────────────────────┬────────────────────────────────┐
│  Source schema     │  Target dataset schema         │
│  (samples + meta)  │  (image, bbox[], class)        │
│                    │                                │
│  blob_hash    ●──→──● image                         │
│  width        ●──→──● image_width                   │
│  ann.bbox[]   ●──→──● annotations[].bbox            │
│  ann.label    ●─?─→─● ??? (unmapped, warning)       │
└────────────────────┴────────────────────────────────┘
[ Preview transformed sample ]  [ Save mapping ]
```

- **Reuse `@xyflow/react`** runtime as the workflow canvas, with a constrained node-type set
  (`source-field`, `target-field`, `transform`). Smaller maintenance surface.
- Each mapping line shows live preview of the transform output for one sample below.
- Unmapped target fields show a `warning` badge ("missing required field `class`") — per
  `error-clarity`.
- **Save mapping** creates a new dataset commit (append-only, matches the `Commit` model).

---

## 9. Graphs across detail pages

No top-level "Graphs" page — embed charts where the data lives.

| Context | Charts |
|---|---|
| Project Dashboard | sparkline (data growth), bar (runs per day) |
| Dataset View | class distribution histogram, bbox area heat, train/val/test split donut |
| Model Detail | training/val loss line, confusion matrix heatmap, PR curve |
| Run View | gantt (per-step duration), resource gauges (cpu/mem when available) |

**Library: Recharts** — composable React components, SVG-based (easy to add accessible
patterns), ~56kb gzipped, MIT. Considered ECharts (heavier, imperative), Victory and nivo
(heavier, more opinionated). Recharts is the sweet spot for the dataset sizes here.

Apply per chart:
- `chart-1..8` tokens (already defined).
- Tooltip on hover/tap with exact values.
- Empty state for no-data periods (not a blank axis).
- Skeleton matching the chart shape during load.
- `aria-label` summary describing the key insight.
- Interactive legend (`legend-interactive`).
- Pair color with shape / pattern for colorblind users.

---

## Mobile strategy

This product is read-only on phones by design — you don't build ML pipelines on a phone.

| Breakpoint | Behavior |
|---|---|
| ≤640px | Render `Projects`, Project Dashboard, Run View (live status), Model Detail. Everything else: "Open on desktop for full editor" with a share-link button (`empty-nav-state`). |
| ≥768px | Full UI; sidebar collapses to top tab bar. |
| ≥1280px | Full sidebar + 3-pane layouts. |

---

## 10. Tech additions  (concrete)

Add to `services/frontend/package.json`:

```json
"recharts":                       "^2.13",
"@tanstack/react-virtual":        "^3.10",
"@radix-ui/react-dialog":         "^1.1",
"@radix-ui/react-dropdown-menu":  "^2.1",
"@radix-ui/react-tabs":           "^1.1",
"@radix-ui/react-toast":          "^1.2",
"@radix-ui/react-tooltip":        "^1.1",
"@radix-ui/react-visually-hidden":"^1.1",
"cmdk":                           "^1.0",
"sonner":                         "^1.5",
"lucide-react":                   "^0.460",
"date-fns":                       "^4.1"
```

Already present (no change): `@xyflow/react`, `@rjsf/core`, `@rjsf/validator-ajv8`,
`@tanstack/react-query`, `zustand`, `react-router-dom`, `axios`, `clsx`, `tailwind-merge`.

Decision: **don't add `shadcn/ui` wholesale.** Cherry-pick Radix primitives and style with
Tailwind tokens directly — less code, full control, matches the existing token system.

---

## Information architecture (full route map)

```
/                                       → redirect /projects
/projects                               list
/projects/:id                           Project Dashboard           §4 — new
/projects/:id/data-sources              list
/projects/:id/data-sources/:dsId        Data Source + Files         §5 — new
/projects/:id/samples                   Sample Browser
/projects/:id/ontologies                Ontology editor             — new
/projects/:id/datasets                  list
/projects/:id/datasets/:dsId            Dataset View
/projects/:id/datasets/:dsId/mapping    Data Mapping                §8 — new
/projects/:id/datasets/:dsId/commits/:c Commit detail
/projects/:id/workflows                 list
/projects/:id/workflows/:wfId           Workflow Builder            §6 — overhaul
/projects/:id/runs                      Runs history                — new
/projects/:id/runs/:runId               Run View                    §7 — new
/projects/:id/models                    list
/projects/:id/models/:mId               Model detail
/projects/:id/settings                  Project settings
```

---

## Open decisions

- **Ontology editor**: tree view + flat search, or pure tree? Defer until §8 lands — the
  Mapping editor will inform what shape the schema view should take.
- **Runs history filter set**: status × workflow × initiator × date range? Confirm against
  what the API filters already support before designing.
- **Theme as URL param**: should `?theme=dark` override localStorage so screenshots of bug
  reports come with a forced theme? Low priority; deferable.

---

## Done-criteria for each step

A step is "done" when:

1. New routes are reachable and behave per spec.
2. All affected pages pass `npm run typecheck` and `npm run lint` clean.
3. Build (`npm run build`) succeeds; the bundle hasn't regressed by more than 15%.
4. The Quick Reference §1–§3 (accessibility + touch + performance) checklist passes for the
   new surfaces.
5. Dark mode renders correctly (verify by toggling, not by inferring).
6. The smallest target viewport (375px wide) still works for routes that are supposed to
   work there per the mobile strategy.
