# CVOps — Roadmap

Living document. **Last full audit: 2026-10-10** (every open issue re-verified against `dev` @ `54a0e24`).

> **GitHub is authoritative for *what*; this file is authoritative for *order*.**
> Every piece of remaining work is exactly one issue in exactly one `EPIC-n` milestone.
> This file says which to do first and records what is unscheduled or undecided.
> If an issue here is closed, or an open issue is missing, fix this file.

Related: product vision [`docs/VISION.md`](./docs/VISION.md) · full spec [`docs/MASTER_PLAN.md`](./docs/MASTER_PLAN.md) ·
frontend design [`docs/frontend-design-plan.md`](./docs/frontend-design-plan.md) · design sharp edges
[`docs/09-gaps-and-considerations.md`](./docs/09-gaps-and-considerations.md) · Keycloak plan
[`docs/15-keycloak-user-management.md`](./docs/15-keycloak-user-management.md). Superseded plans live in
[`docs/archive/`](./docs/archive/).

---

## 1. Where things stand

| Area | State | Notes |
|---|---|---|
| API routers | ✅ implemented | Two tenant leaks remain (#32, #33) |
| Workflow engine | ⚠️ works for preprocessing-queue steps | **Workflows with `train`/`auto_label` never finish** (#182); no crash recovery (#187) |
| Steps (`packages/steps`) | ✅ all implemented | `extract_frames`, `commit_dataset`, `export_yolo`, `train`, `human_review`, `auto_label`, `import_dataset`; `evaluate` not started (#79) |
| Workers | ✅ preprocessing / training / cvat | Only preprocessing has retries/DLQ (#189) |
| Auth | ✅ local JWT + JTI blacklist | RBAC never enforced (#72); Keycloak migration planned (#164) |
| Frontend | ✅ most pages built | Feature gaps in EPIC-2, quality in EPIC-3; zero page-level tests (#66) |
| CI | ✅ api, workers, frontend unit (green since #201/#202) | No e2e (#93), no worker lint (#94), no image builds (#95) |
| Observability | ⚠️ partial | Loki/Prometheus scrape exists; no structured logs, correlation ids, tracing |
| Security / prod | ❌ early | Root containers + api `docker.sock` (#34), import path traversal (#185), no TLS/backups/k8s |

## 2. Now — in this order

The two-person split of this list (file ownership, sync points) is pinned as **#204**; phase 2 (Keycloak + frontend features) follows in **#208**.

0. **Keep CI meaningful.** CI restored on 2026-10-10 (#201, #202: pinned ruff/mypy, stale tests fixed); the automated Claude review and SonarQube jobs were dropped (#203). `dev` is protected: `api`, `ruff + mypy (services/api)` and the frontend job are required, plus one approval.
1. **Make workflows finish.** #182 training-queue steps never advance the parent · #183 `auto_label` not registered on worker-training. *(P0 — without these, train/auto-label pipelines are unusable.)*
2. **Close the tenant and container holes.** #32 CVAT router cross-org · #33 + #84 idempotency reuse is cross-tenant (one migration) · #185 import reads arbitrary worker paths · #34 root containers + api `docker.sock`.
3. **Stop corrupting data.** #184 auto_label writes xyxy boxes · #107 UI commit path ignores `by_source_group`.
4. **Engine reliability.** #187 crash recovery · #188 cancel/retry semantics · #189 retries/DLQ in worker-common · #83 per-job watchdog · #77 rollback edge case.

## 3. Next / later — by milestone

| Milestone | Open work (rough priority order) |
|---|---|
| [EPIC-12 Engine reliability](https://github.com/Hozi-AI/CVOps/milestone/12) | #182, #183, #187, #188, #189, #83, #84, #77 |
| [EPIC-9 Security & production](https://github.com/Hozi-AI/CVOps/milestone/9) | #32, #33, #185, #34, #186, #103 TLS, #104 backups, #105 blob GC, #106 Helm |
| [EPIC-5 Steps & workers](https://github.com/Hozi-AI/CVOps/milestone/5) | #184, #79 evaluate, #80 select_for_review, #191 typed ports, #198 workflow templates |
| [EPIC-10 Data model](https://github.com/Hozi-AI/CVOps/milestone/10) | #107, #111 detection schema + `track_id`, #138 auto-create classes, #108 near-dup, #109 ontology remap, #112 retention |
| [EPIC-4 Backend API](https://github.com/Hozi-AI/CVOps/milestone/4) | #192 thumbnails + diff, #75 events, #76 test gaps, #190 rename/delete, #195 by-class stats |
| [EPIC-1 Known UX bugs](https://github.com/Hozi-AI/CVOps/milestone/1) | #50 |
| [EPIC-2 Frontend features](https://github.com/Hozi-AI/CVOps/milestone/2) | #149, #52 ontology lifecycle, #148 sortable samples, #57, #58 logs (+API), #56, #55 refs UI, #53 commit DAG, #60 model compare, #62, #199, #196, #197 |
| [EPIC-3 Frontend quality](https://github.com/Hozi-AI/CVOps/milestone/3) | #64 UI primitives (blocks #56, #60), #113 error states, #66 page tests, #65 a11y, #68 validation, #69 code-split |
| [EPIC-11 Auth & Keycloak](https://github.com/Hozi-AI/CVOps/milestone/11) | epic #164: #165 → #166 → #167 + #168 → #169 → #170; then #72 RBAC, #61 members UI, #71 worker auth |
| [EPIC-7 CI / CD](https://github.com/Hozi-AI/CVOps/milestone/7) | #92 pre-commit → ruff/tsc, #93 e2e, #94 worker lint, #95 image pipeline |
| [EPIC-6 Infra](https://github.com/Hozi-AI/CVOps/milestone/6) | #87 vendor CVAT config, #89 `.dockerignore`, #90 Dockerfiles, #193 Tilt watch, #194 stray migration |
| [EPIC-8 Observability](https://github.com/Hozi-AI/CVOps/milestone/8) | #96 structured logs → #97 correlation ids → #99 tracing, #100 error tracking |

## 4. Decisions and open questions

### Decided (2026-10-10)

1. **Workers never call the API.** Workers advance workflows in-process, writing straight to Postgres and Redis (#182, as worker-cvat already does). `/internal/*` is reject-by-default (#71). The only service call left, API → model-deployer, keeps its shared `WORKER_TOKEN` until #166 replaces it with a Keycloak service-account client. No other shared-secret mechanism is to be built.
2. **Deleted datasets become tombstones; lineage never breaks** (#149). Dataset routes return 404 for a soft-deleted dataset. References from models, runs and exports keep resolving and show the name with a "deleted" badge. Deleting is never blocked. Commits and blobs live until the retention/GC policy removes them (#112 → #105).
3. **Class order is immutable within an ontology version** (#52). A class's position is its YOLO class id. Reorder, add or retire creates a new version; display name and colour are editable in place. Exports use the commit's pinned version.
4. **After the foundation sprint (#204), phase 2 (#208)** runs two tracks: alon1s on Keycloak (EPIC-11), Michaelmo12 on EPIC-2 frontend features. #167 (backend cut-over) and #168 (frontend login) merge on the same day.
5. **Observability (#96 → #97) is one sweep by one person, after phase 2.** It touches nearly every module, so it must not run alongside feature work.

### Still open

1. **Observability backend** beyond the dev box's Loki/Prometheus (#96–#100).
2. **K8s flavour**: managed vs k3s/Rancher (#106).
3. **model-deployer**: keep, or fold into worker-cvat now that `MODEL_DEPLOYER_URL` points there (#34).

## 5. Unscheduled ideas (no issue on purpose)

Taken from MASTER_PLAN, docs/09 and the archived plans; promote one to an issue when it is about to be worked.

- Pascal VOC import (COCO/YOLO/raw are done)
- FiftyOne view of a commit
- Marketing site (`services/website`, frontend-design-plan Phase G)
- Workflow canvas diff mode (frontend-design-plan §4.5)
- PII / anonymisation pass (docs/09 §D)
- Inter-annotator agreement / `needs_second_review` (docs/09 §B)
- Versioned payload and export-manifest formats (docs/09 §C)
- Object-storage lifecycle tiering, CDN, `commit_samples` partitioning (MASTER_PLAN Phase 3)
- GPU queue concurrency / backpressure (MASTER_PLAN Phase 3)
- AD/LDAP federation, break-glass admin, CAC login (archived `auth-design.md`; dropped by doc 15)
- Frontend caller for `/annotated-uploads/confirm` — the API path stays for programmatic clients; the UI uses the ImportDataset flow (see [`docs/16-dataset-import.md`](./docs/16-dataset-import.md))

## 6. Conventions

- Priority labels are `P0`–`P3` only; type labels `bug`/`feature`/`chore`/`test`/`security`/`infra`/`ci`/`observability`/`refactor`/`documentation`; area labels `api`/`frontend`/`steps`/`worker`.
- A milestone **is** the epic. The `epic` label is used only for a tracking issue inside one (today: #164).
- Don't leave an issue half-true. Either append a `Status` section listing what remains, or close it and open a granular follow-up linked from the closing comment.
