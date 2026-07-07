"""events: add org_id for multi-tenant activity feed

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-07 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0003'
down_revision: Union[str, None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('events', sa.Column('org_id', sa.Uuid(), nullable=True))
    op.create_index('ix_events_org_id_created_at', 'events', ['org_id', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_events_org_id_created_at', table_name='events')
    op.drop_column('events', 'org_id')
