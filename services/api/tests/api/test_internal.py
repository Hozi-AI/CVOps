"""
Tests for the /internal/health endpoint.

The health endpoint executes SELECT 1 against the database. To avoid
importing cvops_steps (which is not installed in the test environment)
the lifespan context is bypassed by building a lightweight test app that
mounts only the internal router against the test database.

asyncio_mode = "auto" in pyproject.toml means async test functions and
async fixtures are collected and driven by pytest-asyncio automatically.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch

from cvops_api.routers import internal


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Minimal test app — avoids cvops_steps import in main.py lifespan
# ---------------------------------------------------------------------------


def _build_test_app() -> FastAPI:
    """
    Create a minimal FastAPI instance that registers only the internal router.
    This bypasses the lifespan in cvops_api.main which tries to import cvops_steps.
    """
    test_app = FastAPI()
    test_app.include_router(internal.router, prefix="/internal")
    return test_app


_test_app = _build_test_app()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(postgres_url: str) -> AsyncClient:
    """
    AsyncClient pointed at the lightweight test app.

    The internal router's health endpoint calls async_session_factory from
    cvops_api.db.session. We patch that factory to use a session bound to
    the test database so the SELECT 1 hits a real but ephemeral Postgres
    instance rather than the default settings URL.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from contextlib import asynccontextmanager

    test_engine = create_async_engine(postgres_url, echo=False)
    test_factory = async_sessionmaker(test_engine, expire_on_commit=False, class_=AsyncSession)

    @asynccontextmanager
    async def _override_session_factory():  # type: ignore[return]
        async with test_factory() as sess:
            yield sess

    with patch("cvops_api.routers.internal.async_session_factory", test_factory):
        async with AsyncClient(
            transport=ASGITransport(app=_test_app),
            base_url="http://test",
        ) as c:
            yield c

    await test_engine.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_health_returns_200(client: AsyncClient) -> None:
    """GET /internal/health must respond with HTTP 200."""
    response = await client.get("/internal/health")

    assert response.status_code == 200


async def test_health_response_has_status_ok(client: AsyncClient) -> None:
    """GET /internal/health must return the JSON body {"status": "ok"}."""
    response = await client.get("/internal/health")

    assert response.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# CVAT webhook bridge — validates the HMAC signature and, on a job-completion
# event, XADDs a {kind: cvat_sync} doorbell onto the cvat stream. fake_redis
# (from the top-level conftest) replaces the process-global Redis.
# ---------------------------------------------------------------------------


async def test_webhook_missing_secret_503(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.delenv("CVAT_WEBHOOK_SECRET", raising=False)
    resp = await client.post(
        "/internal/cvat/webhook", content=b"{}", headers={"X-Signature-256": "sha256=x"}
    )
    assert resp.status_code == 503


async def test_webhook_bad_signature_401(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.setenv("CVAT_WEBHOOK_SECRET", "s3cret")
    resp = await client.post(
        "/internal/cvat/webhook", content=b"{}", headers={"X-Signature-256": "sha256=deadbeef"}
    )
    assert resp.status_code == 401


async def test_webhook_completion_enqueues(client: AsyncClient, fake_redis, monkeypatch) -> None:
    monkeypatch.setenv("CVAT_WEBHOOK_SECRET", "s3cret")
    body = json.dumps(
        {"event": "update:job", "job": {"task_id": 4242, "state": "completed"}}
    ).encode()
    resp = await client.post(
        "/internal/cvat/webhook", content=body, headers={"X-Signature-256": _sign("s3cret", body)}
    )
    assert resp.status_code == 200
    assert resp.json() == {"received": "queued"}

    assert await fake_redis.xlen("cvat") == 1
    _msg_id, fields = (await fake_redis.xrange("cvat"))[0]
    assert fields == {"kind": "cvat_sync", "cvat_task_id": "4242"}


async def test_webhook_non_completion_ignored(client: AsyncClient, fake_redis, monkeypatch) -> None:
    monkeypatch.setenv("CVAT_WEBHOOK_SECRET", "s3cret")
    body = json.dumps(
        {"event": "update:job", "job": {"task_id": 4242, "state": "in progress"}}
    ).encode()
    resp = await client.post(
        "/internal/cvat/webhook", content=body, headers={"X-Signature-256": _sign("s3cret", body)}
    )
    assert resp.status_code == 200
    assert resp.json() == {"received": "ignored"}
    assert await fake_redis.xlen("cvat") == 0
