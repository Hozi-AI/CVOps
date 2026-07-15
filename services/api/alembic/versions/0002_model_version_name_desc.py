"""add name and description to model_versions

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-15
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("model_versions", sa.Column("name", sa.Text(), nullable=True))
    op.add_column("model_versions", sa.Column("description", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("model_versions", "description")
    op.drop_column("model_versions", "name")
