"""Tests for AUTH_MODE=none — no login, every request is the fixed dev user."""

import uuid

import pytest
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


async def test_tokenless_request_is_the_dev_user(dev_email, session):
    first = await get_current_user(_request(), None, session)
    second = await get_current_user(_request(), None, session)

    assert first.id == second.id
    assert first.email == dev_email
    membership = (
        await session.execute(select(Membership).where(Membership.user_id == first.id))
    ).scalar_one()
    assert membership.role == "owner"


async def test_viewer_dependency_is_the_dev_user(dev_email, session):
    user = await get_viewer_user(_request(), None, session)
    assert user.email == dev_email


def test_validate_secrets_rejects_none_mode_without_insecure_opt_in():
    with pytest.raises(RuntimeError, match="AUTH_MODE=none"):
        validate_secrets(Settings(AUTH_MODE="none", ALLOW_INSECURE_DEFAULTS=False))


def test_validate_secrets_allows_none_mode_for_local_dev():
    validate_secrets(Settings(AUTH_MODE="none", ALLOW_INSECURE_DEFAULTS=True))
