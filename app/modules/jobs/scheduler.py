import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.modules.jobs.service import run_reconciliation
from app.modules.payments.service import monnify_client

logger = logging.getLogger("kontributa.scheduler")

scheduler = AsyncIOScheduler()


async def _scheduled_reconciliation() -> None:
    async with AsyncSessionLocal() as db:
        await run_reconciliation(db, monnify_client)


def start_scheduler() -> None:
    if scheduler.running:
        return
    scheduler.add_job(
        _scheduled_reconciliation,
        "interval",
        minutes=settings.RECONCILIATION_INTERVAL_MINUTES,
        id="reconciliation",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("reconciliation scheduler started, interval=%d minutes", settings.RECONCILIATION_INTERVAL_MINUTES)


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
