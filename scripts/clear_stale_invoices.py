import asyncio
import logging
from sqlalchemy import text
from app.core.db import AsyncSessionLocal

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("kontributa.clear_stale_invoices")

async def clear_stale_invoices():
    """Clears cached invoice/checkout session fields for all pending contributions
    whose invoice amount was set prior to a purse amount edit or top-up request.
    This forces a fresh Paystack invoice to be generated for the correct amount on next payment attempt.
    """
    async with AsyncSessionLocal() as session:
        query = text("""
            UPDATE contributions
            SET 
                invoice_id = NULL,
                account_number = NULL,
                bank_name = NULL,
                invoice_expires_at = NULL,
                platform_fee_percent_applied = NULL
            WHERE status = 'pending' AND invoice_id IS NOT NULL;
        """)
        result = await session.execute(query)
        await session.commit()
        logger.info(f"Successfully cleared {result.rowcount} stale invoice(s).")

if __name__ == "__main__":
    asyncio.run(clear_stale_invoices())
