"""member removed_at soft delete

Revision ID: c0f1e55cffb4
Revises: 3e9236d91032
Create Date: 2026-07-26 00:02:12.388666

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c0f1e55cffb4'
down_revision: Union[str, None] = '3e9236d91032'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('members', sa.Column('removed_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('members', 'removed_at')
