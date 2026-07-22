import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.modules.contributions.models import ActorType, Contribution, ContributionStatus
from app.modules.contributions.service import ContributionService
from app.modules.notifications.service import NotificationService
from app.modules.payments.service import MonnifyClient, MonnifyError

logger = logging.getLogger("kontributa.reconciliation")


async def run_reconciliation(
    db: AsyncSession,
    monnify: MonnifyClient,
    purse_id: Optional[UUID] = None,
    notifications: Optional[NotificationService] = None,
) -> tuple[int, int]:
    """Finds every Contribution still pending past a safe threshold and
    queries Monnify's transaction status directly for each -- covers a
    webhook that was dropped or delayed. Applies the exact same
    apply_payment_confirmation/expire_if_needed transitions the webhook
    handler and generate-invoice use, so there is one place, not two,
    that decides how a Contribution's status changes.

    Safe to call repeatedly: only contributions still `pending` are ever
    queried or touched, so a contribution already resolved by an earlier
    run (this one or the scheduled one) is simply skipped, not re-applied.

    Returns (checked_count, updated_count).
    """
    threshold = datetime.now(timezone.utc) - timedelta(
        minutes=settings.RECONCILIATION_PENDING_THRESHOLD_MINUTES
    )

    stmt = select(Contribution).where(
        Contribution.status == ContributionStatus.PENDING,
        Contribution.invoice_id.is_not(None),
        Contribution.updated_at <= threshold,
    )
    if purse_id is not None:
        stmt = stmt.where(Contribution.purse_id == purse_id)

    contributions = (await db.execute(stmt)).scalars().all()
    contribution_service = ContributionService(db)

    checked = 0
    updated = 0

    for contribution in contributions:
        checked += 1

        contribution = await contribution_service.expire_if_needed(contribution, notifications)
        if contribution.status != ContributionStatus.PENDING:
            updated += 1
            continue

        try:
            tx_status = await monnify.get_transaction_status(contribution.invoice_id)
        except MonnifyError:
            logger.warning("reconciliation: Monnify query failed for contribution %s", contribution.id)
            continue

        if tx_status.payment_status != "PAID":
            continue

        result = await contribution_service.apply_payment_confirmation(
            contribution,
            tx_status.amount_paid,
            tx_status.paid_on,
            ActorType.RECONCILIATION_JOB,
            "Reconciliation job",
            notifications,
        )
        if result is not None:
            updated += 1

    logger.info("reconciliation run: checked=%d updated=%d", checked, updated)
    return checked, updated
