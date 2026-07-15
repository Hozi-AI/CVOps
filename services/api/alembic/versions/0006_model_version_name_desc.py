"""add name and description to model_versions

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-15
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("model_versions", sa.Column("name", sa.Text(), nullable=True))
    op.add_column("model_versions", sa.Column("description", sa.Text(), nullable=True))
    # trained_on_commit_id was mistakenly created NOT NULL in 0001; make it nullable
    # so manual model uploads (no associated commit) succeed.
    op.alter_column("model_versions", "trained_on_commit_id", nullable=True)


def downgrade() -> None:
    op.alter_column("model_versions", "trained_on_commit_id", nullable=False)
    op.drop_column("model_versions", "description")
    op.drop_column("model_versions", "name")
