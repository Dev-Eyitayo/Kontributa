import json
import logging
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.contributions.models import ActorType, Contribution, ContributionStatus
from app.modules.contributions.service import ContributionService
from app.modules.notifications.service import NotificationService, SendByteClient
from app.modules.payments.service import parse_monnify_datetime
from app.modules.payouts.models import Payout, PayoutStatus
from app.modules.payouts.service import PayoutService
from app.modules.webhooks.models import WebhookEvent
from app.modules.webhooks.schemas import CollectionEventData, TransferEventData

logger = logging.getLogger("kontributa.webhooks")


class WebhookService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def store_event(
        self, provider_event_id: str, raw_payload: str, signature_valid: bool
    ) -> tuple[WebhookEvent, bool]:
        """Inserts the raw event keyed by provider_event_id. If that id was
        already seen (duplicate delivery), returns the existing row and
        is_new=False instead of inserting again -- relies on the DB unique
        constraint rather than a check-then-insert race."""
        event = WebhookEvent(
            provider_event_id=provider_event_id,
            raw_payload=raw_payload,
            signature_valid=signature_valid,
        )
        self.db.add(event)
        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            result = await self.db.execute(
                select(WebhookEvent).where(WebhookEvent.provider_event_id == provider_event_id)
            )
            return result.scalar_one(), False

        await self.db.refresh(event)
        return event, True

    async def mark_processed(self, event_id: UUID, error: str | None = None) -> None:
        event = await self.db.get(WebhookEvent, event_id)
        if event is None:
            return
        event.processed = True
        event.processing_error = error
        await self.db.commit()


def _extract_collection_event(raw_payload: str) -> CollectionEventData | None:
    payload = json.loads(raw_payload)
    if payload.get("eventType") != "SUCCESSFUL_TRANSACTION":
        return None

    event_data = payload.get("eventData", {})
    paid_on_raw = event_data.get("paidOn")
    return CollectionEventData(
        transaction_reference=event_data.get("transactionReference", ""),
        payment_reference=event_data.get("paymentReference", ""),
        amount_paid=Decimal(str(event_data.get("amountPaid", "0"))),
        payment_status=event_data.get("paymentStatus", ""),
        paid_on=parse_monnify_datetime(paid_on_raw) if paid_on_raw else None,
    )


def _extract_transfer_event(raw_payload: str) -> TransferEventData | None:
    payload = json.loads(raw_payload)
    event_type = payload.get("eventType", "")
    if event_type not in ("SUCCESSFUL_DISBURSEMENT", "FAILED_DISBURSEMENT", "REVERSED_DISBURSEMENT"):
        return None

    event_data = payload.get("eventData", {})
    return TransferEventData(
        reference=event_data.get("reference", ""),
        success=event_type == "SUCCESSFUL_DISBURSEMENT",
        reason=event_data.get("reason") if event_type != "SUCCESSFUL_DISBURSEMENT" else None,
    )


async def process_transfer_webhook_event(
    event_id: UUID, session_factory: async_sessionmaker, sendbyte: SendByteClient
) -> None:
    """Mirrors process_collection_webhook_event for disbursement/transfer
    callbacks -- same dedup-by-provider_event_id, same "only pending/
    processing rows get touched" idempotency guard, same single shared
    decision point (PayoutService.apply_transfer_confirmation)."""
    async with session_factory() as db:
        service = WebhookService(db)
        event = await db.get(WebhookEvent, event_id)
        if event is None:
            return

        data = _extract_transfer_event(event.raw_payload)
        if data is None:
            await service.mark_processed(event_id, error="not a transfer event")
            return

        result = await db.execute(select(Payout).where(Payout.monnify_transfer_ref == data.reference))
        payout = result.scalar_one_or_none()
        if payout is None:
            await service.mark_processed(event_id, error="no payout matches transfer reference")
            return

        if payout.status != PayoutStatus.PROCESSING:
            await service.mark_processed(event_id, error=f"payout already {payout.status.value}, skipped")
            return

        notifications = NotificationService(db, sendbyte)
        payout = await PayoutService(db).apply_transfer_confirmation(
            payout, data.success, data.reason, notifications
        )

        await service.mark_processed(event_id)
        logger.info(
            "processed transfer webhook event %s for payout %s -> %s", event_id, payout.id, payout.status.value
        )


async def process_collection_webhook_event(
    event_id: UUID, session_factory: async_sessionmaker, sendbyte: SendByteClient
) -> None:
    """Runs as a FastAPI background task, after the 202 response has already
    been sent -- opens its own DB session since the request-scoped one may
    be gone by the time this executes. Takes the session factory explicitly
    (bound to whatever engine the triggering request's session used) rather
    than importing a hardcoded global, so it works the same way in tests
    (per-test engine) and production (the app's single long-lived engine)."""
    async with session_factory() as db:
        service = WebhookService(db)
        event = await db.get(WebhookEvent, event_id)
        if event is None:
            return

        data = _extract_collection_event(event.raw_payload)
        if data is None:
            await service.mark_processed(event_id, error="not a collection event")
            return

        result = await db.execute(select(Contribution).where(Contribution.invoice_id == data.payment_reference))
        contribution = result.scalar_one_or_none()
        if contribution is None:
            await service.mark_processed(event_id, error="no contribution matches payment reference")
            return

        if contribution.status != ContributionStatus.PENDING:
            await service.mark_processed(
                event_id, error=f"contribution already {contribution.status.value}, skipped"
            )
            return

        # Single shared decision point for pending -> paid/flagged_for_review --
        # the reconciliation job (Phase 4) calls this exact same method.
        notifications = NotificationService(db, sendbyte)
        contribution = await ContributionService(db).apply_payment_confirmation(
            contribution, data.amount_paid, data.paid_on, ActorType.WEBHOOK, "Monnify webhook", notifications
        )

        await service.mark_processed(event_id)
        logger.info(
            "processed webhook event %s for contribution %s -> %s",
            event_id,
            contribution.id,
            contribution.status.value,
        )
