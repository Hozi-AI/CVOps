"""add model_artifacts table

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-15
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_artifacts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("created_by", UUID(as_uuid=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "model_version_id",
            UUID(as_uuid=True),
            sa.ForeignKey("model_versions.id"),
            nullable=False,
        ),
        sa.Column(
            "blob_hash",
            sa.Text(),
            sa.ForeignKey("blobs.hash"),
            nullable=False,
        ),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_model_artifacts_model_version_id", "model_artifacts", ["model_version_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_model_artifacts_model_version_id")
    op.drop_table("model_artifacts")
