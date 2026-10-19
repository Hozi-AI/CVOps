"""add cvat_deployments table

Records which org owns each Nuclio function deployed to CVAT, so the CVAT
router can scope list/delete/annotate per tenant. Ownership is exclusive: the
partial unique index allows one live row per function_name.

Revision ID: 0008
Revises: cfb17149867a
Create Date: 2026-10-14
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0008"
down_revision = "cfb17149867a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cvat_deployments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("created_by", UUID(as_uuid=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("org_id", UUID(as_uuid=True), sa.ForeignKey("orgs.id"), nullable=False),
        sa.Column("function_name", sa.Text(), nullable=False),
        sa.Column("model_name", sa.Text(), nullable=False),
        sa.Column(
            "model_version_id",
            UUID(as_uuid=True),
            sa.ForeignKey("model_versions.id"),
            nullable=True,
        ),
    )
    op.create_index("ix_cvat_deployments_id", "cvat_deployments", ["id"])
    op.create_index("ix_cvat_deployments_deleted_at", "cvat_deployments", ["deleted_at"])
    op.create_index("ix_cvat_deployments_org_id", "cvat_deployments", ["org_id"])
    op.create_index("ix_cvat_deployments_function_name", "cvat_deployments", ["function_name"])
    op.create_index(
        "ix_cvat_deployments_model_version_id", "cvat_deployments", ["model_version_id"]
    )
    op.create_index(
        "uq_cvat_deployments_function_name",
        "cvat_deployments",
        ["function_name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("cvat_deployments")
