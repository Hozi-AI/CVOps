import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cvops_api.db.base import Base, EntityBase


class Run(Base, EntityBase):
    __tablename__ = "runs"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    parent_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("runs.id"), nullable=True)
    workflow_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("workflows.id"), nullable=True
    )
    workflow_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    step_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    step_type: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="pending",
        server_default="pending",
        index=True,
    )
    input_refs: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    output_refs: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    metrics: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    logs_blob_hash: Mapped[Optional[str]] = mapped_column(ForeignKey("blobs.hash"), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<Run id={self.id!r} kind={self.kind!r} status={self.status!r}"
            f" project_id={self.project_id!r}>"
        )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    actor_id: Mapped[Optional[uuid.UUID]] = mapped_column(nullable=True)
    actor_type: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(nullable=True, index=False)

    __table_args__ = (
        Index("ix_events_entity", "entity_type", "entity_id", "created_at"),
        Index("ix_events_org_id_created_at", "org_id", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<Event id={self.id!r} action={self.action!r}"
            f" entity_type={self.entity_type!r} entity_id={self.entity_id!r}>"
        )
