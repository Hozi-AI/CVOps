from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from cvops_api.config import settings, validate_secrets
from cvops_api.db.session import engine
from cvops_api.routers import (
    auth,
    orgs,
    projects,
    data_sources,
    samples,
    collections,
    tags,
    ontologies,
    datasets,
    workflows,
    runs,
    models,
    training_containers,
    registry as registry_router,
    internal,
    cvat,
    viewer,
    events,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    from cvops_api.core.redis_client import init_redis, close_redis

    validate_secrets(settings)
    await init_redis()
    try:
        from cvops_steps import register_all

        register_all()
    except ImportError:
        pass
    yield
    await close_redis()
    await engine.dispose()


app = FastAPI(
    title="CVOps API",
    version="0.1.0",
    description="ML lifecycle dashboard — dataset versioning, workflow orchestration, training dispatch.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Public API version prefix. The API *owns* this prefix — the nginx edge and
# the Vite dev proxy pass `/api/v1/*` through unchanged (no rewrite), so the
# path is defined in exactly one place and OpenAPI docs describe real URLs.
API_V1 = "/api/v1"


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    """Liveness probe — process is up. Stable and unversioned so container/k8s
    health checks never depend on the API version. (DB *readiness* is the
    deeper check at `/api/v1/internal/health`.)"""
    return {"status": "ok"}


# ── Routers — all public API under /api/v1 ─────────────────────────────────
app.include_router(auth.router, prefix=f"{API_V1}/auth", tags=["auth"])
app.include_router(registry_router.router, prefix=f"{API_V1}/registry", tags=["registry"])
app.include_router(orgs.router, prefix=f"{API_V1}/orgs", tags=["orgs"])
app.include_router(projects.router, prefix=f"{API_V1}/projects", tags=["projects"])
app.include_router(data_sources.router, prefix=API_V1, tags=["data-sources"])
app.include_router(samples.router, prefix=API_V1, tags=["samples"])
app.include_router(collections.router, prefix=API_V1, tags=["collections"])
app.include_router(tags.router, prefix=API_V1, tags=["tags"])
app.include_router(ontologies.router, prefix=API_V1, tags=["ontologies"])
app.include_router(datasets.router, prefix=API_V1, tags=["datasets"])
app.include_router(workflows.router, prefix=API_V1, tags=["workflows"])
app.include_router(runs.router, prefix=API_V1, tags=["runs"])
app.include_router(events.router, prefix=API_V1, tags=["events"])
app.include_router(models.router, prefix=API_V1, tags=["models"])
app.include_router(training_containers.router, prefix=API_V1, tags=["training-containers"])
app.include_router(internal.router, prefix=f"{API_V1}/internal", tags=["internal"])
app.include_router(cvat.router, prefix=API_V1, tags=["cvat"])
# Viewer is a human-facing server-rendered page, not part of the versioned JSON
# API — it stays at root (`/dataset`) with its own nginx pass-through.
app.include_router(viewer.router, prefix="", tags=["viewer"])
