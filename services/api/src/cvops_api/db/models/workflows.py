import uuid
from typing import Any
from sqlalchemy import ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from cvops_api.db.base import Base, EntityBase


class Workflow(Base, EntityBase):
    __tablename__ = "workflows"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        comment=("Shape: {name, steps:[{id, type, config, inputs}], edges:[[from,to]]}"),
    )
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )

    __table_args__ = (
        # ponytail: partial index so soft-deleted workflows don't block name reuse
        Index(
            "uq_workflows_project_name",
            "project_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<Workflow id={self.id!r} project_id={self.project_id!r} "
            f"name={self.name!r} version={self.version!r}>"
        )
