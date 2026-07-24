"""platform settings and custodian mode kill switch

Revision ID: b2e4f6a8d0c1
Revises: 7a3f9c2e1b44
Create Date: 2026-07-24 21:00:00.000000

Adds the platform_settings singleton table: custodian_mode_enabled (off by
default -- a fresh deployment is Direct-only until a platform admin turns
Custodian back on) and platform_fee_percent (0 by default, not yet wired
into any split calculation -- persisted and admin-editable, nothing more).
No row is seeded here; PlatformSettingsService.get_or_create() creates the
one-and-only row lazily on first read/write, same as any other on-demand
singleton in this codebase.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b2e4f6a8d0c1'
down_revision: Union[str, None] = '7a3f9c2e1b44'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'platform_settings',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('custodian_mode_enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('platform_fee_percent', sa.Numeric(5, 2), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table('platform_settings')
