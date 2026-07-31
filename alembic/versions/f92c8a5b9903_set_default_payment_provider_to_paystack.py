"""set default payment provider to paystack

Revision ID: f92c8a5b9903
Revises: f91c7a4b8902
Create Date: 2026-07-31 05:03:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f92c8a5b9903'
down_revision: Union[str, None] = 'f91c7a4b8902'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Update server defaults for platform_settings
    op.alter_column('platform_settings', 'paystack_enabled', server_default='1')
    op.alter_column('platform_settings', 'active_payment_provider', server_default='paystack')

    # 2. Update existing platform_settings rows to use paystack as active_payment_provider and paystack_enabled = true
    op.execute("UPDATE platform_settings SET active_payment_provider = 'paystack', paystack_enabled = true")

    # 3. Update server defaults for settlement_accounts
    op.alter_column('settlement_accounts', 'payment_provider', server_default='paystack')


def downgrade() -> None:
    op.alter_column('platform_settings', 'paystack_enabled', server_default='0')
    op.alter_column('platform_settings', 'active_payment_provider', server_default='monnify')
    op.alter_column('settlement_accounts', 'payment_provider', server_default='monnify')
