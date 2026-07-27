"""contribution group_admin owner

Revision ID: f3a8c2d19b4e
Revises: 61f60ea08b78
Create Date: 2026-07-27 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3a8c2d19b4e'
down_revision: Union[str, None] = '61f60ea08b78'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('contributions', sa.Column('group_admin_id', sa.UUID(), nullable=True))
    op.create_index(op.f('ix_contributions_group_admin_id'), 'contributions', ['group_admin_id'], unique=False)
    op.create_foreign_key(
        'contributions_group_admin_id_fkey', 'contributions', 'group_admins', ['group_admin_id'], ['id']
    )
    op.alter_column('contributions', 'member_id', existing_type=sa.UUID(), nullable=True)
    op.create_unique_constraint(
        'uq_contribution_purse_group_admin', 'contributions', ['purse_id', 'group_admin_id']
    )
    op.create_check_constraint(
        'ck_contribution_exactly_one_owner',
        'contributions',
        '(member_id IS NOT NULL AND group_admin_id IS NULL) OR (member_id IS NULL AND group_admin_id IS NOT NULL)',
    )


def downgrade() -> None:
    op.drop_constraint('ck_contribution_exactly_one_owner', 'contributions', type_='check')
    op.drop_constraint('uq_contribution_purse_group_admin', 'contributions', type_='unique')
    op.alter_column('contributions', 'member_id', existing_type=sa.UUID(), nullable=False)
    op.drop_constraint('contributions_group_admin_id_fkey', 'contributions', type_='foreignkey')
    op.drop_index(op.f('ix_contributions_group_admin_id'), table_name='contributions')
    op.drop_column('contributions', 'group_admin_id')
