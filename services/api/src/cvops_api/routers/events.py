from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from cvops_api.core.auth import get_current_user
from cvops_api.core.pagination import encode_cursor, decode_cursor_parts
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import User
from cvops_api.db.models.runs import Event
from cvops_api.schemas.runs import ActivityEventOut
from cvops_api.schemas.samples import CursorPage

router = APIRouter()


@router.get("/events", response_model=CursorPage[ActivityEventOut])
async def list_events(
    entity_type: str | None = Query(None),
    action: str | None = Query(None),
    cursor: str | None = Query(None),
    limit: int = Query(50, le=200),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> CursorPage[ActivityEventOut]:
    # actor_email has no FK — explicit outerjoin
    q = (
        select(Event, User.email.label("actor_email"))
        .outerjoin(User, Event.actor_id == User.id)
        .where(Event.org_id == current_user.org_id)
    )
    if entity_type is not None:
        q = q.where(Event.entity_type == entity_type)
    if action is not None:
        q = q.where(Event.action == action)
    if cursor is not None:
        ts_str, id_str = decode_cursor_parts(cursor)
        try:
            cursor_ts = datetime.fromisoformat(ts_str)
            cursor_id = uuid.UUID(id_str)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid pagination cursor") from exc
        q = q.where(tuple_(Event.created_at, Event.id) < (cursor_ts, cursor_id))

    q = q.order_by(Event.created_at.desc(), Event.id.desc()).limit(limit + 1)
    rows = (await session.execute(q)).all()

    next_cursor: str | None = None
    if len(rows) == limit + 1:
        last_ev = rows[limit - 1][0]
        next_cursor = encode_cursor(f"{last_ev.created_at.isoformat()}|{last_ev.id}")
        rows = rows[:limit]

    items = [
        ActivityEventOut(
            id=ev.id,
            created_at=ev.created_at,
            actor_id=ev.actor_id,
            actor_type=ev.actor_type,
            actor_email=email,
            entity_type=ev.entity_type,
            entity_id=ev.entity_id,
            action=ev.action,
            payload=ev.payload,
        )
        for ev, email in rows
    ]
    return CursorPage(items=items, next_cursor=next_cursor)
