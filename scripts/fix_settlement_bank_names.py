"""
Database Migration & Data Adjustment Script for Kontributa Settlement Accounts

Updates settlement_account records where bank_name is stored as a raw OPay bank code
('999992' or '100004') to the full display name 'OPay Digital Services Limited (OPay)'.

Usage (from repo root):
    python scripts/fix_settlement_bank_names.py
"""
import asyncio
import sys
from pathlib import Path

# Ensure root directory is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.core.config import settings

# Import models to ensure foreign keys resolve properly in SQLAlchemy ORM
from app.modules.auth.models import User  # noqa: F401
from app.modules.group_admins.models import GroupAdmin  # noqa: F401
from app.modules.organizations.models import Group, Organization  # noqa: F401
from app.modules.settlement.models import SettlementAccount

TARGET_OPAY_CODES = {"999992", "100004"}
OPAY_FULL_NAME = "OPay Digital Services Limited (OPay)"


async def fix_settlement_bank_names() -> None:
    print("=" * 60)
    print(" KONTRIPUTA SETTLEMENT ACCOUNT BANK NAME DATA FIX ")
    print("=" * 60)

    engine = create_async_engine(settings.DATABASE_URL)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with session_factory() as db:
        result = await db.execute(select(SettlementAccount))
        accounts = result.scalars().all()
        print(f"Found {len(accounts)} settlement account(s) in database.")

        updated_count = 0

        for account in accounts:
            raw_bank_name = (account.bank_name or "").strip()
            
            # Check for raw OPay codes
            if raw_bank_name in TARGET_OPAY_CODES:
                print(f"  [Group ID: {account.group_id}] Updating bank_name: '{raw_bank_name}' -> '{OPAY_FULL_NAME}'")
                account.bank_name = OPAY_FULL_NAME
                updated_count += 1

        if updated_count > 0:
            await db.commit()
            print(f"\n✅ Successfully updated {updated_count} OPay settlement account record(s).")
        else:
            print("\nℹ️ All settlement accounts already have valid human-readable bank names.")

    await engine.dispose()
    print("Cleanup complete.")


if __name__ == "__main__":
    asyncio.run(fix_settlement_bank_names())
