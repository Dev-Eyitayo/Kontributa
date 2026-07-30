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
from app.modules.realtime.service import RealtimeService

logger = logging.getLogger("kontributa.reconciliation")


async def run_reconciliation(
    db: AsyncSession,
    monnify: MonnifyClient,
    purse_id: Optional[UUID] = None,
    notifications: Optional[NotificationService] = None,
    realtime: Optional[RealtimeService] = None,
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
    if purse_id is not None:
        stmt = select(Contribution).where(
            Contribution.status.in_([ContributionStatus.PENDING, ContributionStatus.EXPIRED]),
            Contribution.invoice_id.is_not(None),
            Contribution.purse_id == purse_id,
        )
    else:
        threshold = datetime.now(timezone.utc) - timedelta(
            minutes=settings.RECONCILIATION_PENDING_THRESHOLD_MINUTES
        )
        stmt = select(Contribution).where(
            Contribution.status.in_([ContributionStatus.PENDING, ContributionStatus.EXPIRED]),
            Contribution.invoice_id.is_not(None),
            Contribution.updated_at <= threshold,
        )

    contributions = (await db.execute(stmt)).scalars().all()
    contribution_service = ContributionService(db)
    if realtime is None:
        realtime = RealtimeService()

    checked = 0
    updated = 0

    from app.modules.payments.service import get_payment_provider
    from app.modules.purses.models import Purse
    from app.modules.settlement.models import SettlementAccount

    for contribution in contributions:
        checked += 1

        purse = await db.get(Purse, contribution.purse_id)
        provider = monnify
        if purse is not None:
            settlement = (
                await db.execute(select(SettlementAccount).where(SettlementAccount.group_id == purse.group_id))
            ).scalar_one_or_none()
            if settlement is not None:
                provider = get_payment_provider(getattr(settlement, "payment_provider", "monnify"))

        try:
            tx_status = await provider.get_transaction_status(contribution.invoice_id)
        except Exception as exc:
            logger.warning("reconciliation: %s query failed for contribution %s: %s", getattr(provider, "provider_name", "provider"), contribution.id, exc)
            tx_status = None

        if tx_status and tx_status.payment_status == "PAID":
            result = await contribution_service.apply_payment_confirmation(
                contribution,
                tx_status.amount_paid,
                tx_status.paid_on,
                ActorType.RECONCILIATION_JOB,
                "Reconciliation job",
                notifications,
                realtime,
            )
            if result is not None:
                updated += 1
            continue

        contribution = await contribution_service.expire_if_needed(contribution, notifications, realtime)

    logger.info("reconciliation run: checked=%d updated=%d", checked, updated)
    return checked, updated
