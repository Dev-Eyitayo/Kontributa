"""dual settlement mode (direct vs custodian)

Revision ID: 7a3f9c2e1b44
Revises: 1906c4e5d012
Create Date: 2026-07-24 10:00:00.000000

Adds settlement_mode (a new "settlement_mode" enum: custodian | direct) and
direct_sub_account_code to settlement_accounts. Every existing row is a
custodian-mode account by definition (direct mode didn't exist before this),
so the backfill sets settlement_mode='custodian' explicitly rather than
leaving it to infer from an absent value -- there is no silent default in
the application layer going forward, this backfill is the one-time
exception for rows that predate the concept of a mode at all.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7a3f9c2e1b44'
down_revision: Union[str, None] = '1906c4e5d012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

settlement_mode_enum = sa.Enum('custodian', 'direct', name='settlement_mode')


def upgrade() -> None:
    settlement_mode_enum.create(op.get_bind(), checkfirst=True)
    op.add_column(
        'settlement_accounts',
        sa.Column('settlement_mode', settlement_mode_enum, nullable=False, server_default='custodian'),
    )
    op.alter_column('settlement_accounts', 'settlement_mode', server_default=None)
    op.add_column('settlement_accounts', sa.Column('direct_sub_account_code', sa.String(length=50), nullable=True))


def downgrade() -> None:
    op.drop_column('settlement_accounts', 'direct_sub_account_code')
    op.drop_column('settlement_accounts', 'settlement_mode')
    settlement_mode_enum.drop(op.get_bind(), checkfirst=True)
