"""JWT utilities and the get_current_user FastAPI dependency."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, UTC
from typing import Any, cast

from fastapi import Depends, HTTPException, Query, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.config import settings
from cvops_api.core.proxy_auth import resolve_proxy_user
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import User

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
_oauth2 = OAuth2PasswordBearer(tokenUrl="/auth/token")
oauth2_scheme = _oauth2
# get_current_user raises its own 401 so AUTH_MODE=proxy can run without a token.
_oauth2_optional = OAuth2PasswordBearer(tokenUrl="/auth/token", auto_error=False)

_REVOKED_PREFIX = "revoked:"


def hash_password(plain: str) -> str:
    return str(_pwd_ctx.hash(plain))


def verify_password(plain: str, hashed: str) -> bool:
    return bool(_pwd_ctx.verify(plain, hashed))


def create_access_token(subject: str) -> str:
    expire = datetime.now(UTC) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return str(
        jwt.encode(
            {"sub": subject, "exp": expire, "type": "access", "jti": uuid.uuid4().hex},
            settings.JWT_SECRET,
            algorithm=settings.JWT_ALGORITHM,
        )
    )


def create_refresh_token(subject: str) -> str:
    expire = datetime.now(UTC) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    return str(
        jwt.encode(
            {"sub": subject, "exp": expire, "type": "refresh", "jti": uuid.uuid4().hex},
            settings.JWT_SECRET,
            algorithm=settings.JWT_ALGORITHM,
        )
    )


def decode_token(token: str) -> dict[str, Any]:
    try:
        return cast(
            dict[str, Any],
            jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]),
        )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def blacklist_token(jti: str, exp: int) -> None:
    """Store a token JTI in Redis until it would have expired naturally."""
    from cvops_api.core.redis_client import get_redis

    ttl = max(1, exp - int(datetime.now(UTC).timestamp()))
    await get_redis().setex(f"{_REVOKED_PREFIX}{jti}", ttl, "1")


async def is_blacklisted(jti: str) -> bool:
    from cvops_api.core.redis_client import get_redis

    return await get_redis().exists(f"{_REVOKED_PREFIX}{jti}") == 1


async def resolve_user(token: str, session: AsyncSession) -> User:
    """Decode a token, enforce the blacklist, and load the active user.

    Shared by `get_current_user` (header-only) and `get_viewer_user` (also
    accepts the token as a query param for plain browser navigation)."""
    payload = decode_token(token)

    jti = payload.get("jti")
    if jti and await is_blacklisted(jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )
    result = await session.execute(select(User).where(User.id == uuid.UUID(user_id)))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )
    return user


async def get_current_user(
    request: Request,
    token: str | None = Depends(_oauth2_optional),
    session: AsyncSession = Depends(get_session),
) -> User:
    if settings.AUTH_MODE == "proxy":
        return await resolve_proxy_user(request, session)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await resolve_user(token, session)


async def get_viewer_user(
    request: Request,
    token: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Auth dependency for server-rendered viewer pages.

    Accepts the JWT either as a `?token=` query param (so a plain browser
    navigation works) or as the usual `Authorization: Bearer` header."""
    if settings.AUTH_MODE == "proxy":
        return await resolve_proxy_user(request, session)
    if token is None:
        header = request.headers.get("authorization", "")
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer" and value:
            token = value
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await resolve_user(token, session)
