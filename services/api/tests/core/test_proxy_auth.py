"""Tests for AUTH_MODE=proxy — identity from the nginx + oauth2-proxy edge."""

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from starlette.requests import Request

from cvops_api.config import Settings, settings, validate_secrets
from cvops_api.core.auth import get_current_user, get_viewer_user
from cvops_api.db.models.auth import Membership, User

SECRET = "s" * 40


def _request(headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": raw})


def _edge_headers(email: str, secret: str = SECRET) -> dict[str, str]:
    return {"X-Proxy-Secret": secret, "X-Auth-Request-Email": email}


@pytest.fixture
def proxy_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AUTH_MODE", "proxy")
    monkeypatch.setattr(settings, "PROXY_SHARED_SECRET", SECRET)


async def test_first_login_provisions_user_then_reuses_it(proxy_mode, session):
    email = f"{uuid.uuid4().hex}@example.com"

    first = await get_current_user(_request(_edge_headers(email)), None, session)
    second = await get_current_user(_request(_edge_headers(email)), None, session)

    assert first.id == second.id
    assert first.email == email
    assert first.password_hash is None
    membership = (
        await session.execute(select(Membership).where(Membership.user_id == first.id))
    ).scalar_one()
    assert membership.role == "owner"
    assert membership.org_id == first.org_id


async def test_viewer_dependency_uses_edge_identity(proxy_mode, session):
    email = f"{uuid.uuid4().hex}@example.com"
    user = await get_viewer_user(_request(_edge_headers(email)), None, session)
    assert user.email == email


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Auth-Request-Email": "spoof@example.com"},  # no secret
        _edge_headers("spoof@example.com", secret="wrong" * 8),
        {"X-Proxy-Secret": SECRET},  # no identity
    ],
)
async def test_rejects_unproven_or_missing_identity(proxy_mode, session, headers):
    with pytest.raises(HTTPException) as exc:
        await get_current_user(_request(headers), None, session)
    assert exc.value.status_code == 401
    count = (
        await session.execute(select(User).where(User.email == "spoof@example.com"))
    ).scalar_one_or_none()
    assert count is None


async def test_empty_configured_secret_never_matches(monkeypatch, session):
    monkeypatch.setattr(settings, "AUTH_MODE", "proxy")
    monkeypatch.setattr(settings, "PROXY_SHARED_SECRET", "")
    headers = {"X-Proxy-Secret": "", "X-Auth-Request-Email": "a@example.com"}
    with pytest.raises(HTTPException) as exc:
        await get_current_user(_request(headers), None, session)
    assert exc.value.status_code == 401


async def test_local_mode_ignores_edge_headers(session):
    assert settings.AUTH_MODE == "local"
    with pytest.raises(HTTPException) as exc:
        await get_current_user(_request(_edge_headers("a@example.com")), None, session)
    assert exc.value.status_code == 401


def test_validate_secrets_requires_strong_proxy_secret_even_when_insecure_allowed():
    s = Settings(AUTH_MODE="proxy", PROXY_SHARED_SECRET="short", ALLOW_INSECURE_DEFAULTS=True)
    with pytest.raises(RuntimeError, match="PROXY_SHARED_SECRET"):
        validate_secrets(s)
