"""workflows: partial unique index on (project_id, name) where deleted_at is null

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-12 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0004'
down_revision: Union[str, None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("uq_workflows_project_name", "workflows", type_="unique")
    op.create_index(
        "uq_workflows_project_name",
        "workflows",
        ["project_id", "name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_workflows_project_name", table_name="workflows")
    op.create_unique_constraint(
        "uq_workflows_project_name", "workflows", ["project_id", "name"]
    )
