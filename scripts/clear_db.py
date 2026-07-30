"""
Database Clearing Script for Kontributa Backend

Completely removes all data from the database by truncating all tables
and resetting primary key sequences. Also clears Redis cache/tokens.

Usage (from repo root):
    python scripts/clear_db.py
    
    # Or non-interactively without confirmation prompt:
    python scripts/clear_db.py --yes
"""
import argparse
import asyncio
import sys
from pathlib import Path

# Ensure root directory is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.core.redis import get_redis

# Import all models to ensure Base.metadata.tables is fully populated
from app.modules.audit import models as _audit_models  # noqa: F401
from app.modules.auth import models as _auth_models  # noqa: F401
from app.modules.contributions import models as _contribution_models  # noqa: F401
from app.modules.group_admins import models as _group_admin_models  # noqa: F401
from app.modules.invites import models as _invite_models  # noqa: F401
from app.modules.members import models as _member_models  # noqa: F401
from app.modules.notifications import models as _notifications_models  # noqa: F401
from app.modules.organizations import models as _org_models  # noqa: F401
from app.modules.payouts import models as _payout_models  # noqa: F401
from app.modules.platform_settings import models as _platform_models  # noqa: F401
from app.modules.purses import models as _purse_models  # noqa: F401
from app.modules.settlement import models as _settlement_models  # noqa: F401
from app.modules.webhooks import models as _webhook_models  # noqa: F401
from app.core.db import Base


async def clear_database(skip_prompt: bool = False) -> None:
    db_url = settings.DATABASE_URL
    print("=" * 60)
    print(" WARNING: KONTRIPUTA DATABASE CLEARING SCRIPT ")
    print("=" * 60)
    print(f"Target Database: {db_url}")
    print("This will PERMANENTLY ERASE all data in the database!")
    print("=" * 60)

    if not skip_prompt:
        confirm = input("Type 'YES' to proceed with wiping the database: ")
        if confirm.strip() != "YES":
            print("Operation cancelled.")
            sys.exit(0)

    print("\nStarting database cleanup...")

    engine = create_async_engine(db_url)

    async with engine.begin() as conn:
        dialect = engine.dialect.name

        if dialect == "postgresql":
            # List of all tables to truncate
            tables = [table.name for table in Base.metadata.sorted_tables]
            if tables:
                tables_str = ", ".join(f'"{t}"' for t in tables)
                print(f"Truncating {len(tables)} PostgreSQL tables with CASCADE...")
                await conn.execute(text(f"TRUNCATE TABLE {tables_str} RESTART IDENTITY CASCADE;"))
                print("Tables truncated successfully.")
            else:
                print("No metadata tables found to truncate.")

            # Re-initialize default audit_chain_head row if present
            try:
                await conn.execute(text("INSERT INTO audit_chain_head (id, last_row_hash) VALUES (1, NULL) ON CONFLICT (id) DO NOTHING;"))
            except Exception:
                pass

        else:
            # SQLite or other DBs: disable foreign keys, delete from all tables
            print(f"Clearing database ({dialect})...")
            await conn.execute(text("PRAGMA foreign_keys = OFF;"))
            for table in reversed(Base.metadata.sorted_tables):
                print(f"  Clearing table: {table.name}")
                await conn.execute(text(f'DELETE FROM "{table.name}";'))
            await conn.execute(text("PRAGMA foreign_keys = ON;"))

    await engine.dispose()

    # Clear Redis tokens/cache
    try:
        redis = await get_redis()
        await redis.flushdb()
        await redis.aclose()
        print("Redis cache and token store cleared.")
    except Exception as e:
        print(f"Notice: Redis flush skipped or encountered an error ({e})")



    print("\nDatabase cleanup complete! All data has been removed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Completely wipe all data from Kontributa database.")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt and proceed immediately.")
    args = parser.parse_args()

    asyncio.run(clear_database(skip_prompt=args.yes))
