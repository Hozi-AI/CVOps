"""No-login mode (AUTH_MODE=none): every request is one fixed dev user.

For single-user local dev only. validate_secrets refuses this mode unless
ALLOW_INSECURE_DEFAULTS=true, because anyone who can reach the API becomes
this user.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.config import settings
from cvops_api.db.models.auth import Membership, Org, User


async def resolve_dev_user(session: AsyncSession) -> User:
    """Load the dev user, creating it with a personal org on first use."""
    email = settings.DEV_USER_EMAIL
    user = await _get_user(session, email)
    if user is None:
        user = await _provision_user(session, email)
    return user


async def _get_user(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def _provision_user(session: AsyncSession, email: str) -> User:
    """Create a personal org + owner membership, like /auth/register.

    Two concurrent first requests race on the unique constraints; the loser
    rolls back and reads the winner's row."""
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
