"""purge custodian mode and enforce direct mode

Revision ID: f91c7a4b8902
Revises: e91c7a4b8901
Create Date: 2026-07-29 09:58:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f91c7a4b8902'
down_revision: Union[str, None] = 'e91c7a4b8901'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add account_name column to settlement_accounts
    op.add_column('settlement_accounts', sa.Column('account_name', sa.String(length=255), nullable=True))
    
    # 2. Update any existing custodian settlement accounts to direct
    op.execute("UPDATE settlement_accounts SET settlement_mode = 'direct' WHERE settlement_mode = 'custodian'")
    
    # 3. Set server default for settlement_mode column to direct
    op.alter_column('settlement_accounts', 'settlement_mode', server_default='direct')


def downgrade() -> None:
    pass
