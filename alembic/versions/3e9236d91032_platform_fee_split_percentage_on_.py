"""platform fee split percentage on contributions

Revision ID: 3e9236d91032
Revises: 091c9af478ba
Create Date: 2026-07-25 21:38:26.487643

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3e9236d91032'
down_revision: Union[str, None] = '091c9af478ba'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'contributions', sa.Column('platform_fee_percent_applied', sa.Numeric(5, 2), nullable=True)
    )

    op.alter_column(
        'platform_settings', 'platform_fee_percent',
        server_default='1',
    )
    # platform_fee_percent was never live-editable before this feature --
    # the only row in production is still sitting at the old server
    # default of 0, so this is a safe one-time backfill to the new
    # default, not an override of any admin's real choice.
    op.execute("UPDATE platform_settings SET platform_fee_percent = 1 WHERE platform_fee_percent = 0")


def downgrade() -> None:
    op.alter_column(
        'platform_settings', 'platform_fee_percent',
        server_default='0',
    )
    op.drop_column('contributions', 'platform_fee_percent_applied')
