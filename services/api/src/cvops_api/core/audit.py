"""
G4 — Generic append-only event log.
Every meaningful mutation calls emit_event(). Powers audit trail,
lineage graph, and the activity feed from one source.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def emit_event(
    session: AsyncSession,
    *,
    actor_id: str | uuid.UUID | None,
    actor_type: str,  # "user" | "service" | "system"
    entity_type: str,  # "project" | "commit" | "run" | "annotation_revision" | ...
    entity_id: str | uuid.UUID,
    action: str,  # "created" | "run.started" | "branch.advanced" | ...
    payload: dict[str, Any] | None = None,
    org_id: uuid.UUID | None = None,
) -> None:
    """
    Insert one row into the events table within the current transaction.
    Does not commit — the caller owns the transaction boundary.
    """
    await session.execute(
        text(
            """
            INSERT INTO events
                (id, actor_id, actor_type, entity_type, entity_id, action, payload, org_id, created_at)
            VALUES
                (:id, :actor_id, :actor_type, :entity_type, :entity_id, :action,
                 cast(:payload as jsonb), :org_id, now())
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "actor_id": str(actor_id) if actor_id else None,
            "actor_type": actor_type,
            "entity_type": entity_type,
            "entity_id": str(entity_id),
            "action": action,
            "payload": __import__("json").dumps(payload) if payload else "{}",
            "org_id": str(org_id) if org_id else None,
        },
    )
