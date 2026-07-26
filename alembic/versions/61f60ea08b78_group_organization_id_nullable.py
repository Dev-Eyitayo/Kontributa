"""group organization_id nullable

Revision ID: 61f60ea08b78
Revises: c0f1e55cffb4
Create Date: 2026-07-26 10:34:42.382400

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '61f60ea08b78'
down_revision: Union[str, None] = 'c0f1e55cffb4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('groups', 'organization_id', existing_type=sa.dialects.postgresql.UUID(as_uuid=True), nullable=True)


def downgrade() -> None:
    op.alter_column('groups', 'organization_id', existing_type=sa.dialects.postgresql.UUID(as_uuid=True), nullable=False)
