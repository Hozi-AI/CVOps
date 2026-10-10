"""Guard against router prefix mistakes.

projects/registry are mounted with a prefix in main.py; if the router also
declares its own prefix the paths double up (/projects/projects). This caught a
real 404 in the ingest flow.
"""

from __future__ import annotations

from cvops_api.main import app


def _paths() -> set[str]:
    # Read the OpenAPI path table rather than app.routes: since FastAPI 0.14x,
    # include_router() wraps routers lazily, so app.routes no longer lists
    # their paths. The OpenAPI table is public API and version-independent.
    return set(app.openapi()["paths"])


def test_projects_not_double_prefixed() -> None:
    paths = _paths()
    assert "/api/v1/projects/" in paths
    assert "/api/v1/projects/projects/" not in paths


def test_registry_not_double_prefixed() -> None:
    paths = _paths()
    assert "/api/v1/registry/types" in paths
    assert "/api/v1/registry/registry/types" not in paths


def test_health_is_root_and_unversioned() -> None:
    paths = _paths()
    assert "/health" in paths
    assert "/api/v1/health" not in paths
