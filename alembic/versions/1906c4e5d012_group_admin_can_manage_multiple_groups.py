"""group admin can manage multiple groups

Revision ID: 1906c4e5d012
Revises: 4a98540
Create Date: 2026-07-24 09:00:00.000000

group_admins.user_id previously had a bare unique index, meaning one User
could only ever administer a single Group, platform-wide. That made it
impossible for an admin to create and actively manage a second group.
This replaces the bare unique index with a non-unique index plus a
composite unique constraint on (user_id, group_id): an admin still can't
hold two rows for the *same* group, but can now administer several
different groups. Mirrors cf671bb40c89's identical change to `members`.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1906c4e5d012'
down_revision: Union[str, None] = 'cf671bb40c89'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index('ix_group_admins_user_id', table_name='group_admins')
    op.create_index(op.f('ix_group_admins_user_id'), 'group_admins', ['user_id'], unique=False)
    op.create_unique_constraint('uq_group_admins_user_id_group_id', 'group_admins', ['user_id', 'group_id'])


def downgrade() -> None:
    op.drop_constraint('uq_group_admins_user_id_group_id', 'group_admins', type_='unique')
    op.drop_index(op.f('ix_group_admins_user_id'), table_name='group_admins')
    op.create_index('ix_group_admins_user_id', 'group_admins', ['user_id'], unique=True)
