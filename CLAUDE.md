# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repo at a glance

CVOps is an ML-lifecycle dashboard that collapses dataset versioning, workflow orchestration, human-in-the-loop labelling gates, blob storage, and training dispatch into one system. It is a small monorepo with four packages:

| Path | Role | Status |
|---|---|---|
| `services/api` | Python 3.12 / FastAPI — REST API, workflow engine, DB layer | implemented |
| `services/frontend` | TypeScript / React 18 + Vite — dashboard UI (auth, all pages, api layer, data-source/frame viewer) | implemented |
| `packages/steps` | Python — step implementations loaded by the engine (`extract_frames`, `commit_dataset`, `export_yolo`, `train`, `human_review` implemented; `auto_label` is a stub) | partial |
| `services/worker-preprocessing` | Python — Redis-Streams worker; consumes the `preprocessing` stream and runs steps out of the API process | implemented |

Standalone CLI prototypes for the same lifecycle steps live in `tools/frame-extractor/` (`extract_frames.py`, `auto_label.py`, `upload_to_cvat.py`).

`services/api/CLAUDE.md` is the authoritative orientation for API work — read it before touching `services/api`.

## Commands

### Stack

Two distinct entry points — keep them straight:

**Inner-loop dev → `tilt up`** (from repo root). Stateful infra (postgres, redis, garage) runs in containers; the API runs as a host `uvicorn --reload` and the frontend as host `npm run dev`. An nginx edge container is also brought up — it serves the placeholder `manifests/nginx/html/index.html` and proxies `/api/v1/*` to the host API (via `host.docker.internal`), reachable at `http://localhost` (and `http://<dev-vm>` for VM-based devs). The API *owns* the `/api/v1` prefix (routers are mounted under it in `main.py`), so both the nginx edge and Vite's `server.proxy` pass `/api/v1/*` through unchanged — no rewrite, prefix defined in one place. Migrations run automatically (`migrate-up`) and `packages/steps` is installed into the API venv (`steps-install`). Host prereqs: Python 3.12+, Node 20+, Docker, ffmpeg. First `tilt up` runs `pip install -e services/api[dev]` and `npm install` automatically.

**Optional heavy stacks (OFF by default).** The CVAT labelling stack (~10 containers + submodule checkout + nuctl + `worker-cvat`) and the training stack (`worker-training`'s multi-GB torch/ultralytics image + MLflow) are the expensive parts, so a plain `tilt up` skips both and brings up only the core (postgres, redis, garage, api, frontend, `worker-preprocessing`, nginx, migrations). Opt in per-run with Tilt args — `tilt up -- --cvat`, `tilt up -- --training`, `tilt up -- --heavy` (both) — or persistently with env vars `CVOPS_ENABLE_CVAT=1` / `CVOPS_ENABLE_TRAINING=1` (shell env or `manifests/.env`). The toggles drive both the host resources and the compose profiles (`worker` + `mlflow`); MLflow is now profile-gated in `docker-compose.yml`, so it comes up only with training (Tilt) or `--profile app/all` (compose).

**Pre-prod / integration → `docker compose` with profiles** (from `manifests/`). Everything containerised.

```bash
cd manifests

docker compose up                                                            # infra only (matches what Tilt uses)
docker compose --profile app up                                              # + api, frontend, nginx, worker-preprocessing (prod-target builds)
docker compose --profile all up                                              # everything

# Force dev-target containers (rare — for reproducing CI failures):
docker compose -f docker-compose.yml -f docker-compose.dev.yml --profile app up
```

All compose, env, and config files live under `manifests/`. Paths inside the compose files are relative to that directory.

The `worker-preprocessing` service runs in the `app`/`all` profiles (and as a host process under `tilt up`), so the ingest flow works end-to-end out of the API process.

### API (`services/api`)

```bash
pip install -e ".[dev]"
pip install "pydantic[email]"            # required for EmailStr schemas

pytest tests/ -q                          # full suite — uses testcontainers (needs Docker)
pytest tests/routers/test_auth.py -v      # single file
pytest tests/ -k "name_fragment"          # single test by name

ruff check src/ tests/
ruff format --check src/ tests/
mypy src/                                 # strict mode

uvicorn cvops_api.main:app --reload --port 8000
```

The test suite spins up a real PostgreSQL via `testcontainers` — there is no in-memory or SQLite fallback. DB layer changes must keep migrations and ORM models in sync; tests will catch divergence.

### Frontend (`services/frontend`)

```bash
npm install
npm run dev          # http://localhost:5173 in dev compose, :3000 in prod compose
npm run build        # tsc -b && vite build
npm run lint         # eslint, --max-warnings 0
npm run typecheck    # tsc --noEmit
```

## Architecture

```
Client ──► nginx ──► FastAPI ──► Depends(get_current_user)  ──► Router handler
                                  │  decode JWT                        │
                                  │  check Redis JTI blacklist         ├──► PostgreSQL (asyncpg)
                                  └  load User                         └──► Garage S3 (presigned URLs)
```

**Single FastAPI app, single process.** `services/api/src/cvops_api/main.py` mounts every router and registers a `lifespan` that initialises Redis and (best-effort) imports `cvops_steps.register_all()` to populate the step registry.

**Persistence layers (must understand together):**

- PostgreSQL holds all relational state — 21 ORM models in `db/models/`, with a single squashed Alembic migration `0001_initial_schema` in `alembic/versions/` (regenerated from the models via `alembic revision --autogenerate`; the incremental chain was collapsed while pre-prod, since tests bootstrap the schema from the models with `Base.metadata.create_all`, not from migrations). Two circular FKs on `projects` (`default_ontology_id`, `default_ingest_workflow_id`) are added as explicit `op.create_foreign_key` calls after all tables exist — autogenerate emits them as `use_alter` constraints that `op.create_table` silently drops, so they must stay manual.
- Garage (S3-compatible object store) holds every byte payload (images, annotations, model weights). Blobs are content-addressed by SHA-256; the API never proxies bytes — clients get presigned PUT/GET URLs. Presigned URLs are signed against a browser-reachable host (derived per-request from the `Host` header, or `S3_PUBLIC_ENDPOINT` if set), not the internal `S3_ENDPOINT`.
- Redis holds the JWT `jti` revocation list and any transient cache.

**Workflow engine** (`engine/coordinator.py`, `engine/ref_resolver.py`, `engine/step.py`):

Steps execute **out of process** on Redis Streams (doorbell) with Postgres as the authority — see `docs/services/redis-streams.md`. There is no in-process `execute_workflow` anymore.

- A workflow is a DAG: `{steps: [...], edges: [...]}` stored on `Workflow.definition`.
- `POST /workflows/{id}/runs` (and the `confirm-upload` auto-trigger) create a `pending` parent run, then call `advance_workflow(session, run_id, actor_id)` **synchronously in-request**. That creates a child `runs` row for every ready step, **freezes** resolved inputs onto `child.input_refs`, and `XADD`s a thin `{job_id, step_type, queue}` message onto each step's queue (default `preprocessing`). It runs no step.
- A per-queue worker (`services/worker-preprocessing`) consumes the stream, claims one `pending` child via `SELECT … FOR UPDATE SKIP LOCKED`, runs it through `process_step`, writes results to PG, `XACK`s, and calls `advance_workflow` again to enqueue whatever became ready.
- Idempotency key `sha256(type + config + resolved inputs)` still reuses outputs of identical prior succeeded child runs (created already `succeeded`, no enqueue).
- `GateException` from a step pauses the run (`status = waiting`); `POST /runs/{id}/gates/{step_id}/resolve` resumes it by calling `advance_workflow`. Any other exception fails the child + parent on a fresh transaction so transient session state can't swallow the error.
- A step's queue is `Step.queue` (empty → `preprocessing`), so routing needs no edit to the step package.
- Step implementations live in `cvops_steps` (separate package, dynamically loaded at startup). If it fails to import, the engine still runs — just with an empty registry.

**Multi-tenancy** is enforced at the query level: every resource has an `org_id` and routers filter on `current_user.org_id`. Do not rely on application-layer checks.

## Project-specific conventions

These are the conventions that are easy to violate without realising it. Most are documented in `services/api/CLAUDE.md`; the highlights:

- **Cursor pagination, always.** Pattern: `WHERE id > cursor_uuid ORDER BY id LIMIT n+1`; cursor is the base64-encoded UUID of the last item. Response shape: `{"items": [...], "next_cursor": "..." | null}`.
- **No raw bytes through the API.** Use `get_storage().get_presigned_get(blob_hash)` / `get_presigned_put(blob_hash)`. Immutable blobs and commits get `Cache-Control: immutable, max-age=31536000`.
- **Soft-delete via `deleted_at: datetime | None`.** List queries must filter `WHERE deleted_at IS NULL`. Exceptions — `AnnotationRevision`, `Event`, `Commit` are append-only; never delete them.
- **API version prefix.** All public routers are mounted under `/api/v1` in `main.py` (the API owns the prefix; the nginx edge and Vite proxy pass it through unchanged). So a route is reachable at `/api/v1/...` (e.g. `/api/v1/projects/{id}/samples`, `/api/v1/auth/token`). The liveness probe `/health` and the human-facing `/dataset` viewer stay at root, unversioned.
- **Auth dependency:** `get_current_user` validates JWT, checks the Redis blacklist, and loads the `User`. Every endpoint except `/auth/*` requires it.
- **Internal endpoints** (`/api/v1/internal/*`) authenticate with the `WORKER_TOKEN` shared secret, not user JWTs.
- **Router path composition.** Some routers (`data_sources`, `samples`, `ontologies`, `datasets`, `workflows`, `runs`, `models`, `training_containers`) define their full paths inline (e.g. `/projects/{id}/samples`) and are mounted with just the `/api/v1` prefix; `auth`, `orgs`, `projects`, `registry`, `internal` add their own segment (e.g. `/api/v1/projects`). The router objects themselves must NOT declare `APIRouter(prefix=…)`, or paths double up (e.g. `/api/v1/projects/projects`).
- **Backend-triggered ingest.** `POST /data-sources/{id}/confirm-upload` registers the blob and, if the project has `default_ingest_workflow_id` set, auto-dispatches that workflow with `params.source_id` and returns its `run_id`. Hash is verified lazily in `extract_frames`, not at confirm. `confirm-upload` reuses an already-registered `Blob` without re-promoting (the add-without-reupload path) and returns `409` if the same content is already a live source in this project (`uq_data_sources_project_blob`).
- **Duplicate-upload check.** The client hashes the file *first*, then `POST /projects/{id}/data-sources/check` (`{blob_hash}` → `{exists, in_current_project, matches}`) probes for an existing org-wide copy so a duplicate is never pushed over the wire. The org-scoped join means cross-org copies are invisible. Exact-hash only; perceptual near-dup is out of scope.
- **No hardcoded colors in the frontend.** Use only the global brand palette and semantic tokens — never raw hex/`rgb()`/named colors in components. The single source of truth is `services/frontend/src/index.css` (CSS custom properties: brand raw palette `--cv-*`, theme-aware semantic tokens `--surface-N` / `--text-X` / `--border*` / `--focus`) surfaced through `services/frontend/tailwind.config.ts` as utility classes. App code reaches for the **semantic tokens** by default (`bg-surface-2`, `text-text-primary`, `border-border`, `text-error`, `bg-iris-500`); these resolve via `var(--…)` and switch on the `data-theme` attribute (dark is the default, light is first-class). Don't write the raw `--cv-*` brand values directly in components, and don't inline literal colors — if a value you need is missing, add a token to `index.css` (and the Tailwind config) rather than hardcoding. This keeps theming consistent and the dark/light themes correct.

## Adding a new step type

1. Subclass `Step` in `cvops_steps` (the `packages/steps` package in this monorepo, installed into the API env separately).
2. Set `type_key`, `config_schema` (JSON Schema), `category`, `is_gate`.
3. Register in `cvops_steps.register_all()` via `registry.register(MyStep())`.
4. The coordinator picks it up at runtime — no API changes needed. Override `queue` on the step to route it to a non-`preprocessing` worker.

## Git workflow (enforced by hooks)

The same conventions are enforced for **both** VCS front-ends — plain `git` and `jj` (Jujutsu, colocated). The rules live once in `scripts/hooks/lib/rules.sh` (provider-neutral); each front-end is a thin wrapper over it, so `git` and `jj` validate identically.

| Provider | Activate | What enforces it |
|---|---|---|
| **git** | `sh scripts/git-setup.sh` (sets `core.hooksPath=.githooks`) | `.githooks/{commit-msg,pre-push,pre-commit,prepare-commit-msg}` |
| **jj** | `sh scripts/jj-setup.sh` (after `jj git init --colocate`) | `jj check` / `jj push` aliases → `scripts/hooks/jj/{validate,pre-push}.sh` |

Why two front-ends: **jj never runs git hooks** — not `commit-msg`, not `pre-commit`, and `jj git push` bypasses `pre-push`. So the jj side re-implements the gates as aliases over the shared rules.

- **Branch / bookmark format:** `[Claude-Bot/]<Type>/<3-8-kebab-word-title>`, types `Feat|Fix|Chore|Docs|Refactor|Style|Test|Lint` (capitalised). git `pre-push` and `jj push` reject anything else and block pushes to `main`, `develop`, `master`.
- **Commit subject:** `<type>: <3-10 word title>` with lowercase types; subject and body separated by a blank line. git `commit-msg` validates each commit at commit time; under jj, `jj check` / `jj push` validate the described commits in `trunk()..@`.
- **Atomic commits.** One commit = one responsibility. Never mix feature + fix, refactor + logic change, or formatting + behaviour. Under jj, `jj split` is the tool for breaking a bundled working-copy change apart.
- **Bot identity.** Claude-authored commits end with `Generated by [<Agent-Role>-Agent] [Claude-Bot] of [@<committer>]` on the final line (blank line before it), where `<committer>` is the committer's own GitHub handle — the `prepare-commit-msg` hook and `scripts/open-pr.sh` resolve it at runtime (`gh api user`, falling back to `git config user.name`). Never hardcode a name, and never write the literal `@me` (it's a real GitHub account — writing it pings a stranger).
- **Pull requests.** Open PRs with the repo helper `scripts/open-pr.sh` (alias `git open-pr`), never hand-rolled `gh pr create` — it enforces these conventions and writes the body in the house format (a why-focused intro, a `## Changes` list, and `Closes #N` lines when the work resolves issues). Enrich the body with the optional, repeatable fields (run `git open-pr -h` for the full list): `-s` adds a why-focused **Summary** line above the Changes list, `-N` adds a bullet under **## Notes** (caveats, follow-ups, migration/deploy steps), and `-T` adds a bullet under **## Testing** (how it was verified); issue refs use `-c '!N'` (→ `Closes #N`) or `-c N` (→ `Relates to #N`). A bare run still emits the minimal `## Changes` + sign-off body. PR (and commit) bodies must **not** contain external/vendor URLs (no "Generated with Claude Code", `claude.com`/`anthropic.com`, or any tool-attribution link); the only sign-off is `— Claude-Bot on behalf of @<committer>` (the helper fills in the author's handle). Feature branches PR into `dev`; promote `dev → main` with `git open-pr -p` (never push straight to a protected branch).

### Using jj on this repo

`jj` is colocated (`.jj` beside `.git`); `git` and `jj` drive the same working copy. Typical flow:

```bash
jj describe -m "feat: ..."                 # set the current change's message
jj bookmark set <Type>/<title> -r @        # name the change (≈ a branch)
jj check                                    # validate messages + bookmark names now
jj push                                     # validates, then `jj git push`
```

Adding a commit type (e.g. `perf`) means editing `RULES_COMMIT_TYPES` in `scripts/hooks/lib/rules.sh` once — both providers pick it up.

### Parallel agents and the shared working copy

A `jj`/`git` working copy has exactly **one** writer. Do **not** run multiple agents (or any concurrent processes that edit files) against the **same** working directory — `jj` snapshots the working copy on every command, so concurrent writers produce *divergent operations*, and reconciling them can silently reset the working copy and drop uncommitted edits. We hit this: two agents editing one workspace in parallel lost their changes to a `reconcile divergent operations` op (recovered only via `jj op restore <snapshot-op>` — `jj op log` is the safety net).

Rules for parallel agent work:

- **One workspace = one writer.** If you fan out agents that *write*, give each its own isolation (a separate `jj workspace add` directory, or the Agent tool's `isolation: "worktree"`), then merge/commit the results. Agents that only *read* can share freely.
- If you can't isolate, run the writing agents **sequentially**, committing between them.
- Splitting by directory (e.g. backend vs frontend) is *not* enough — `jj` snapshots the whole working copy, so disjoint file sets still collide at the operation log.
- After any suspected collision, **don't `jj workspace update-stale` blindly** (it can reset to an empty commit). Check `jj op log` first and `jj op restore` the snapshot that holds the work.

### Validating a workspace's Python against the right code

Editable installs (`pip install -e services/api`) bind `import cvops_api` to a **fixed path** — usually the main checkout, not your `jj workspace`. Running `pytest` from a workspace then tests the *main* code with the *workspace* tests (and `cvops_steps` may be missing entirely), producing fake failures. The sandbox has **no network**, so a fresh `pip install` venv won't work. Instead point `PYTHONPATH` at the workspace sources so they shadow the install:

```bash
WS=/path/to/CVOps-<workspace>
PYTHONPATH="$WS/services/api/src:$WS/packages/steps/src:$WS/packages/worker-common/src" \
  python -m pytest tests/ -q
```

Verify with `python -c "import cvops_api, os; print(os.path.realpath(cvops_api.__file__))"` before trusting any result.

## Reference docs

- `README.md` — product overview, full API table, walkthrough.
- `docs/MASTER_PLAN.md` — full system reference.
- `docs/01-principles-and-architecture.md` through `docs/10-glossary-and-roadmap.md` — design docs.
- `docs/11-extract-frames.md`, `12-auto-label.md`, `13-upload-to-cvat.md` — the three CLI step specs implemented in `tools/frame-extractor/`.
- `services/api/docs/db/` — per-model schema notes.
- `services/api/CLAUDE.md` — API developer orientation, shared deps, auth model.
