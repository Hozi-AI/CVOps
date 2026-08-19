# CVOps — State of the Repo & Roadmap

Living document. **Last full audit: 2026-06-16.**
For the frontend design plan, see [`PLAN.md`](./PLAN.md).
For product vision and the full data model, see [`docs/MASTER_PLAN.md`](./docs/MASTER_PLAN.md).
For the design-level "sharp edges", see [`docs/09-gaps-and-considerations.md`](./docs/09-gaps-and-considerations.md).

> **The live, tracked backlog is now on GitHub.** This document is the narrative
> state-of-the-repo; the actionable, one-per-gap backlog lives as issues + milestones
> (see [§4](#4-live-backlog-github)). When the two disagree, GitHub is authoritative.

---

## TL;DR

**The API is the most mature layer.** Every router in the data-model spec is implemented, the
workflow engine is end-to-end functional (topological sort, idempotency via input-hash, gate
suspend/resume, ref resolution), auth is wired everywhere except `/auth/*` and `/internal/*`, and
the schema is a single squashed Alembic migration covering all ORM models.

**The execution layer runs on Redis Streams, not Celery.** Steps execute out-of-process: a per-queue
worker claims `pending` child runs (`SELECT … FOR UPDATE SKIP LOCKED`), runs them, and re-advances the
DAG. `worker-preprocessing`, `worker-training`, and `worker-cvat` are all implemented, backed by the
shared `packages/worker-common` and `packages/cvat-client`. Of the step implementations in
`packages/steps`, only **`auto_label` is still a stub** — `extract_frames`, `commit_dataset`,
`export_yolo`, `train`, and the `human_review` gate are implemented.

**The frontend is largely built**, not scaffolding: auth, react-query data fetching, the SSE run
client, the workflow builder, the sample browser, and the dataset/commit views all exist. What
remains is **specific feature completion** (ontology editor, commit-graph DAG + diff, run-view graph
mode + logs, sample lightbox + annotation overlay, model compare) and **quality work** (page-level
tests, a11y, a few missing UI primitives, error boundaries).

**Production-readiness is still early.** No structured logging/metrics/tracing, no secret manager, no
TLS, no DB backup story, no k8s/Helm, and CI covers only the API. `model-deployer` works on the happy
path but `POST /deploy` has no auth and the container runs as root.

**Net assessment:** Phase 1 (single-tenant dev loop) is largely functional end-to-end; the remaining
work is feature completion, hardening, observability, and deployment — all tracked as GitHub issues.

> ⚠️ **Correction to the 2026-06-11 audit.** The prior version of this file claimed all six steps were
> `NotImplementedError`, the worker was an empty Celery shell, 13/15 frontend pages were stubs, and
> design tokens were migrated only in `Header.tsx`. All four were stale — see the verified
> reconciliation below.

---

## 1. Reconciliation vs. the 2026-06-11 audit

| Old claim (stale) | Verified today | Tracking |
|---|---|---|
| All 6 steps raise `NotImplementedError` | Only `auto_label` is a stub; the rest are implemented | [#78](https://github.com/Hozi-AI/CVOps/issues/78) |
| Worker is an empty Celery shell; API uses `BackgroundTasks` | Redis-Streams workers all implemented | reliability: [#82](https://github.com/Hozi-AI/CVOps/issues/82), [#83](https://github.com/Hozi-AI/CVOps/issues/83) |
| 13/15 frontend pages are stubs; no react-query; no SSE | Most pages built; react-query + SSE + auth all present | feature gaps → EPIC-2/3 |
| Design tokens migrated only in `Header.tsx` | **Inverted** — only 3 files still hardcode colors | [#69](https://github.com/Hozi-AI/CVOps/issues/69) |
| `any` types in the frontend | **0 found** | — |
| eslint config missing | Still missing | [#91](https://github.com/Hozi-AI/CVOps/issues/91) |
| `.githooks/pre-commit` uses black/flake8 | Still stale (API moved to ruff) | [#92](https://github.com/Hozi-AI/CVOps/issues/92) |
| `model-deployer POST /deploy` unauthed | Confirmed | [#101](https://github.com/Hozi-AI/CVOps/issues/101) |
| Healthchecks only on infra services | Confirmed | [#85](https://github.com/Hozi-AI/CVOps/issues/85) |
| `WORKER_TOKEN` defined but unenforced | Confirmed — worker sends `Bearer`, API never validates | [#71](https://github.com/Hozi-AI/CVOps/issues/71) |

---

## 2. Current-state matrix

| Area | State | Notes |
|---|---|---|
| API routers | ✅ implemented | Thin endpoint-test coverage |
| Workflow engine | ✅ implemented | One rollback edge case |
| Auth + JWT/JTI blacklist | ✅ implemented | RBAC roles defined, never enforced |
| Storage (Garage, presigned) | ✅ implemented | Blob GC unwired |
| `packages/steps` | ⚠️ partial | `auto_label` stub; `evaluate` doc-only; no unit tests |
| Redis-Streams workers | ✅ implemented | Not all in compose; reliability gaps |
| `model-deployer` | ⚠️ works, unsafe | No `/deploy` auth, hardcoded creds, root container |
| Frontend pages | ⚠️ mostly built | Specific features + known UX bugs remain |
| Frontend quality | ⚠️ partial | No page tests, a11y gaps, missing primitives |
| Infra / compose | ⚠️ partial | Healthchecks, worker-cvat, CVAT service, secrets |
| CI/CD | ⚠️ API only | No frontend/worker CI; lint no-op; stale hook |
| Observability | ❌ absent | No logging lib, metrics, tracing |
| Security / prod | ❌ early | No TLS, secrets mgmt, backup, k8s |

---

## 3. Known user-reported bugs

| Issue | Severity | Root cause |
|---|---|---|
| [#48](https://github.com/Hozi-AI/CVOps/issues/48) | P0 | Signup/login renders the FastAPI 422 `detail` **array** as a React child → crash |
| [#49](https://github.com/Hozi-AI/CVOps/issues/49) | P1 | Avatar click logs out immediately; needs an account menu |
| [#50](https://github.com/Hozi-AI/CVOps/issues/50) | P2 | Add-to-collection silently drops duplicates (UX feedback gap) |
| [#51](https://github.com/Hozi-AI/CVOps/issues/51) | P1 | Commit view shows only the delta; needs full membership + expandable diff |

---

## 4. Live backlog (GitHub)

66 issues (**#48–#113**), one per gap, grouped into 10 epic milestones:

| Milestone | Theme | Count |
|---|---|---|
| [EPIC-1](https://github.com/Hozi-AI/CVOps/milestone/1) | Known UX bugs (user-reported) | 4 |
| [EPIC-2](https://github.com/Hozi-AI/CVOps/milestone/2) | Frontend feature completion | 12 |
| [EPIC-3](https://github.com/Hozi-AI/CVOps/milestone/3) | Frontend quality & hardening | 8 |
| [EPIC-4](https://github.com/Hozi-AI/CVOps/milestone/4) | Backend API gaps | 7 |
| [EPIC-5](https://github.com/Hozi-AI/CVOps/milestone/5) | Steps & workers | 7 |
| [EPIC-6](https://github.com/Hozi-AI/CVOps/milestone/6) | Infra / DevOps | 6 |
| [EPIC-7](https://github.com/Hozi-AI/CVOps/milestone/7) | CI / CD | 5 |
| [EPIC-8](https://github.com/Hozi-AI/CVOps/milestone/8) | Observability | 5 |
| [EPIC-9](https://github.com/Hozi-AI/CVOps/milestone/9) | Security & production deployment | 6 |
| [EPIC-10](https://github.com/Hozi-AI/CVOps/milestone/10) | Data-model considerations (design docs) | 6 |

Labels encode **type** (`bug`/`feature`/`chore`/`test`/`security`/`infra`/`ci`/`observability`/`refactor`),
**area** (`frontend`/`api`/`steps`/`worker`/…), and **priority** (`P0`–`P3`).

---

## 5. Suggested order of attack

1. **Stop the bleeding (P0):** [#48](https://github.com/Hozi-AI/CVOps/issues/48) (auth crash),
   [#101](https://github.com/Hozi-AI/CVOps/issues/101) (model-deployer auth).
2. **Close the known-bug set + core safety (P1):** #49, #51, plus
   [#71](https://github.com/Hozi-AI/CVOps/issues/71) (worker token),
   [#88](https://github.com/Hozi-AI/CVOps/issues/88) (secrets/env validation),
   [#96](https://github.com/Hozi-AI/CVOps/issues/96) (structured logging).
3. **MVP feature completion (P1):** ontology editor, sample lightbox, `auto_label`, stream
   reliability, worker-cvat in compose, the CI gates, group-aware split default.
4. **Important (P2):** rest of EPIC-2/3/4 + healthchecks + endpoint tests.
5. **Polish & production (P3):** EPIC-8/9/10 remainder.

---

## 6. Open decisions

1. **Observability stack** — Loki+Grafana vs. CloudWatch vs. Datadog vs. ELK.
2. **K8s flavor** — managed (EKS/GKE) vs. bare-metal (k3s + Rancher; a Rancher kubeconfig already exists).
3. **Frontend a11y test budget** — manual per-PR vs. Playwright + `@axe-core/playwright` in CI.
4. **Ontology editor shape** (still open from `PLAN.md`).
5. **CVAT submodule update cadence** — pin to a tag, automate via Renovate, or freeze.
6. **Default `SplitStrategy`** — confirm `by_source_group` for video (strongly recommended; split-leakage).
