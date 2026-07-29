import asyncio
import logging
from sqlalchemy import text
from app.core.db import AsyncSessionLocal

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("kontributa.migration")

async def migrate_custodian_to_direct():
    """Migrates all legacy settlement accounts set to custodian mode
    over to direct mode, ensuring 100% of groups operate on direct mode."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("UPDATE settlement_accounts SET settlement_mode = 'direct' WHERE settlement_mode = 'custodian'")
        )
        await session.commit()
        logger.info(f"Successfully migrated {result.rowcount} settlement account(s) to direct mode.")

if __name__ == "__main__":
    asyncio.run(migrate_custodian_to_direct())
