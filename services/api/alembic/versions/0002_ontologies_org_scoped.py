"""ontologies: replace project_id with org_id

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-30 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0002'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add org_id as nullable first so we can back-fill before enforcing NOT NULL
    op.add_column('ontologies', sa.Column('org_id', sa.Uuid(), nullable=True))

    # 2. Back-fill from the project's org
    op.execute(
        "UPDATE ontologies o SET org_id = p.org_id "
        "FROM projects p WHERE p.id = o.project_id"
    )

    # 3. Enforce NOT NULL now that all rows are filled
    op.alter_column('ontologies', 'org_id', nullable=False)

    # 4. Add FK + index for org_id
    op.create_foreign_key('fk_ontologies_org_id', 'ontologies', 'orgs', ['org_id'], ['id'])
    op.create_index('ix_ontologies_org_id', 'ontologies', ['org_id'])

    # 5. Drop old project_id constraints and column
    op.drop_constraint('uq_ontologies_project_name', 'ontologies', type_='unique')
    op.drop_index('ix_ontologies_project_id', table_name='ontologies')
    op.drop_constraint('ontologies_project_id_fkey', 'ontologies', type_='foreignkey')
    op.drop_column('ontologies', 'project_id')

    # 6. Add new unique constraint
    op.create_unique_constraint('uq_ontologies_org_name', 'ontologies', ['org_id', 'name'])


def downgrade() -> None:
    op.add_column('ontologies', sa.Column('project_id', sa.Uuid(), nullable=True))
    op.drop_constraint('uq_ontologies_org_name', 'ontologies', type_='unique')
    op.drop_index('ix_ontologies_org_id', table_name='ontologies')
    op.drop_constraint('fk_ontologies_org_id', 'ontologies', type_='foreignkey')
    op.drop_column('ontologies', 'org_id')
    op.create_index('ix_ontologies_project_id', 'ontologies', ['project_id'])
    op.create_unique_constraint('uq_ontologies_project_name', 'ontologies', ['project_id', 'name'])
