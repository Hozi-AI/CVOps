"""Edge-proxy identity (AUTH_MODE=proxy).

nginx asks oauth2-proxy to authenticate every request (`auth_request`) and, on
success, forwards the user's identity as `X-Auth-Request-Email` together with
`X-Proxy-Secret`. The secret proves the request came through nginx — the API
port may be reachable directly, and the identity header alone is spoofable.
See docs/edge-auth.md.
"""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.config import settings
from cvops_api.db.models.auth import Membership, Org, User

SECRET_HEADER = "x-proxy-secret"
EMAIL_HEADER = "x-auth-request-email"


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


async def resolve_proxy_user(request: Request, session: AsyncSession) -> User:
    """Trust the edge's identity headers and load (or JIT-provision) the user."""
    expected = settings.PROXY_SHARED_SECRET
    supplied = request.headers.get(SECRET_HEADER, "")
    # An empty expected secret must never match an empty supplied one.
    if not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise _unauthorized("Not authenticated via edge proxy")

    email = request.headers.get(EMAIL_HEADER, "").strip()
    if not email:
        raise _unauthorized("Edge proxy supplied no user identity")

    return await get_or_provision_user(session, email)


async def get_or_provision_user(session: AsyncSession, email: str) -> User:
    """Load the user by email, creating it with a personal org on first sight.

    Shared by proxy mode and AUTH_MODE=none (the fixed dev user)."""
    user = await _get_user(session, email)
    if user is None:
        user = await _provision_user(session, email)
    if not user.is_active:
        raise _unauthorized("User not found or inactive")
    return user


async def _get_user(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def _provision_user(session: AsyncSession, email: str) -> User:
    """First login: create a personal org + owner membership, like /auth/register.

    The org is named after the (unique) email so it can't collide. Two
    concurrent first requests race on the unique constraints; the loser rolls
    back and reads the winner's row."""
    try:
        org = Org(name=email)
        session.add(org)
        await session.flush()
        user = User(org_id=org.id, email=email, password_hash=None, is_active=True)
        session.add(user)
        await session.flush()
        session.add(Membership(org_id=org.id, user_id=user.id, role="owner"))
        await session.commit()
        return user
    except IntegrityError:
        await session.rollback()
        existing = await _get_user(session, email)
        if existing is None:
            raise
        return existing
