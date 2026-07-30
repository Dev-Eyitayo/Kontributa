"""add paystack and multi-gateway support

Revision ID: e91c7a4b8901
Revises: 091c9af478ba
Create Date: 2026-07-29 09:17:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e91c7a4b8901'
down_revision: Union[str, None] = 'e819f2a03b51'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('platform_settings', sa.Column('monnify_enabled', sa.Boolean(), server_default='1', nullable=False))
    op.add_column('platform_settings', sa.Column('paystack_enabled', sa.Boolean(), server_default='0', nullable=False))
    op.add_column('platform_settings', sa.Column('active_payment_provider', sa.String(length=20), server_default='monnify', nullable=False))

    op.add_column('settlement_accounts', sa.Column('payment_provider', sa.String(length=20), server_default='monnify', nullable=False))


def downgrade() -> None:
    op.drop_column('settlement_accounts', 'payment_provider')
    op.drop_column('platform_settings', 'active_payment_provider')
    op.drop_column('platform_settings', 'paystack_enabled')
    op.drop_column('platform_settings', 'monnify_enabled')
