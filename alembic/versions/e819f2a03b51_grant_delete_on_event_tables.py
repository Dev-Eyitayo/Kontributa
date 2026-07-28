"""cascade delete on group entities and lock event tables

Revision ID: e819f2a03b51
Revises: 89604a1e5448
Create Date: 2026-07-28 18:28:30.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.core.config import settings

# revision identifiers, used by Alembic.
revision: str = 'e819f2a03b51'
down_revision: Union[str, None] = '89604a1e5448'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = settings.APP_DB_ROLE


def upgrade() -> None:
    # 1. Re-enforce append-only lock on event/audit tables for application role
    op.execute(f"REVOKE UPDATE, DELETE ON contribution_events, payout_events FROM {APP_ROLE}")

    # 2. Re-create foreign keys with ON DELETE CASCADE so PostgreSQL engine handles cascading deletes automatically
    fk_configs = [
        ("contribution_events", "contribution_events_contribution_id_fkey", "contribution_id", "contributions", "id"),
        ("payout_events", "payout_events_payout_id_fkey", "payout_id", "payouts", "id"),
        ("payout_allocations", "payout_allocations_payout_id_fkey", "payout_id", "payouts", "id"),
        ("contributions", "contributions_purse_id_fkey", "purse_id", "purses", "id"),
        ("purses", "purses_group_id_fkey", "group_id", "groups", "id"),
        ("payouts", "payouts_group_id_fkey", "group_id", "groups", "id"),
        ("invite_links", "invite_links_group_id_fkey", "group_id", "groups", "id"),
        ("members", "members_group_id_fkey", "group_id", "groups", "id"),
        ("group_admins", "group_admins_group_id_fkey", "group_id", "groups", "id"),
        ("settlement_accounts", "settlement_accounts_group_id_fkey", "group_id", "groups", "id"),
    ]

    for table, constraint_name, fk_col, ref_table, ref_col in fk_configs:
        op.execute(
            f"""
            DO $$
            DECLARE
                r RECORD;
            BEGIN
                FOR r IN (
                    SELECT tc.constraint_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                    WHERE tc.constraint_type = 'FOREIGN KEY'
                      AND tc.table_name = '{table}'
                      AND kcu.column_name = '{fk_col}'
                ) LOOP
                    EXECUTE 'ALTER TABLE ' || quote_ident('{table}') || ' DROP CONSTRAINT ' || quote_ident(r.constraint_name);
                END LOOP;

                EXECUTE 'ALTER TABLE ' || quote_ident('{table}') ||
                        ' ADD CONSTRAINT ' || quote_ident('{constraint_name}') ||
                        ' FOREIGN KEY (' || quote_ident('{fk_col}') || ')' ||
                        ' REFERENCES ' || quote_ident('{ref_table}') || '(' || quote_ident('{ref_col}') || ')' ||
                        ' ON DELETE CASCADE';
            END
            $$;
            """
        )


def downgrade() -> None:
    pass
