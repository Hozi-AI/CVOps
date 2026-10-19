"""Tests for AUTH_MODE: no default, no-login mode, and the startup guards."""

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from starlette.requests import Request

from cvops_api.config import Settings, settings, validate_secrets
from cvops_api.core.auth import get_current_user, get_viewer_user
from cvops_api.db.models.auth import Membership


def _request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "headers": []})


@pytest.fixture
def dev_email(monkeypatch: pytest.MonkeyPatch) -> str:
    email = f"{uuid.uuid4().hex}@example.com"
    monkeypatch.setattr(settings, "AUTH_MODE", "none")
    monkeypatch.setattr(settings, "DEV_USER_EMAIL", email)
    return email


def _real_secrets(**overrides) -> Settings:  # type: ignore[no-untyped-def]
    base = {
        "JWT_SECRET": "a-real-secret-that-is-long-enough-yes",
        "WORKER_TOKEN": "real-worker-token",
        "S3_ACCESS_KEY": "GKrealaccesskey",
        "S3_SECRET_KEY": "realsecretkey",
    }
    base.update(overrides)
    return Settings(**base)


# ── no default ────────────────────────────────────────────────────────────────


def test_auth_mode_has_no_default() -> None:
    assert Settings().AUTH_MODE is None


def test_blank_auth_mode_counts_as_unset() -> None:
    assert Settings(AUTH_MODE="").AUTH_MODE is None


def test_startup_refuses_unset_auth_mode_even_when_insecure_allowed() -> None:
    with pytest.raises(RuntimeError, match="AUTH_MODE is not set"):
        validate_secrets(_real_secrets(ALLOW_INSECURE_DEFAULTS=True))


def test_startup_accepts_local_mode() -> None:
    validate_secrets(_real_secrets(AUTH_MODE="local"))


async def test_unset_mode_at_runtime_is_401_not_a_guess(monkeypatch, session) -> None:
    monkeypatch.setattr(settings, "AUTH_MODE", None)
    with pytest.raises(HTTPException) as exc:
        await get_current_user(None, session)
    assert exc.value.status_code == 401


# ── AUTH_MODE=none ────────────────────────────────────────────────────────────


async def test_none_mode_tokenless_request_is_the_dev_user(dev_email, session) -> None:
    first = await get_current_user(None, session)
    second = await get_current_user(None, session)

    assert first.id == second.id
    assert first.email == dev_email
    membership = (
        await session.execute(select(Membership).where(Membership.user_id == first.id))
    ).scalar_one()
    assert membership.role == "owner"


async def test_none_mode_viewer_dependency_is_the_dev_user(dev_email, session) -> None:
    user = await get_viewer_user(_request(), None, session)
    assert user.email == dev_email


def test_startup_refuses_none_mode_without_insecure_opt_in() -> None:
    with pytest.raises(RuntimeError, match="AUTH_MODE=none"):
        validate_secrets(_real_secrets(AUTH_MODE="none", ALLOW_INSECURE_DEFAULTS=False))


def test_startup_allows_none_mode_for_local_dev() -> None:
    validate_secrets(_real_secrets(AUTH_MODE="none", ALLOW_INSECURE_DEFAULTS=True))


# ── AUTH_MODE=local ───────────────────────────────────────────────────────────


async def test_local_mode_still_requires_a_token(session) -> None:
    assert settings.AUTH_MODE == "local"  # set by the autouse conftest fixture
    with pytest.raises(HTTPException) as exc:
        await get_current_user(None, session)
    assert exc.value.status_code == 401
