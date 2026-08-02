"""multi-modality: nullable dims + modality discriminator

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-02
"""
from alembic import op
import sqlalchemy as sa

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Make image-specific columns optional so non-image samples can be inserted
    op.alter_column('samples', 'width', existing_type=sa.Integer(), nullable=True)
    op.alter_column('samples', 'height', existing_type=sa.Integer(), nullable=True)

    # Modality discriminator on samples ('image' | 'text' | 'sensor' | 'audio')
    op.add_column('samples', sa.Column(
        'modality', sa.Text(), nullable=False,
        server_default='image',
    ))
    # Modality on projects — drives UI, step palette, and annotation type filtering
    op.add_column('projects', sa.Column(
        'modality', sa.Text(), nullable=False,
        server_default='image',
    ))


def downgrade() -> None:
    op.drop_column('projects', 'modality')
    op.drop_column('samples', 'modality')
    op.alter_column('samples', 'height', existing_type=sa.Integer(), nullable=False)
    op.alter_column('samples', 'width', existing_type=sa.Integer(), nullable=False)
