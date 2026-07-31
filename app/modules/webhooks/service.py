import json
import logging
import re
from datetime import datetime
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
from app.modules.realtime.service import RealtimeService
from app.modules.settlement.models import MonnifySettlementLog, SettlementAccount
from app.modules.webhooks.models import WebhookEvent
from app.modules.webhooks.schemas import CollectionEventData, RejectedPaymentEventData, TransferEventData

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

    # 1. Monnify collection event
    if payload.get("eventType") == "SUCCESSFUL_TRANSACTION":
        event_data = payload.get("eventData", {})
        paid_on_raw = event_data.get("paidOn")
        return CollectionEventData(
            transaction_reference=event_data.get("transactionReference", ""),
            payment_reference=event_data.get("paymentReference", ""),
            amount_paid=Decimal(str(event_data.get("amountPaid", "0"))),
            payment_status=event_data.get("paymentStatus", "PAID"),
            paid_on=parse_monnify_datetime(paid_on_raw) if paid_on_raw else None,
        )

    # 2. Paystack collection event
    if payload.get("event") == "charge.success":
        event_data = payload.get("data", {})
        metadata = event_data.get("metadata") or {}

        # Log Paystack split percentage & subaccount details
        subaccount_info = event_data.get("subaccount") or {}
        split_info = event_data.get("split") or {}
        fees_split = event_data.get("fees_split") or {}
        
        split_share = (
            subaccount_info.get("percentage_charge")
            or subaccount_info.get("share")
            or (fees_split.get("params", {}).get("percentage_charge") if isinstance(fees_split, dict) else None)
            or (
                split_info.get("formula", {}).get("subaccounts", [{}])[0].get("share")
                if isinstance(split_info, dict) and isinstance(split_info.get("formula"), dict)
                else None
            )
        )
        subaccount_code = subaccount_info.get("subaccount_code") or subaccount_info.get("subaccount")
        total_amount = event_data.get("amount", 0)
        paystack_fee = event_data.get("fees", 0)

        # Paystack fees_split dictionary holds 'integration' (subaccount share) and 'paystack'
        subaccount_amount = (
            subaccount_info.get("amount")
            or (fees_split.get("integration") if isinstance(fees_split, dict) else None)
        )

        logger.info(
            "Paystack charge.success received: ref=%s, total_amount=%s kobo, subaccount=%s, split_share=%s%%, subaccount_payout=%s kobo (Paystack Fee: %s kobo)",
            event_data.get("reference"),
            total_amount,
            subaccount_code,
            split_share,
            subaccount_amount,
            paystack_fee,
        )

        # 2a. Check explicit metadata invoice_id or referrer URL (e.g. https://paystack.shop/pay/PRQ_wte6pqhc8nbh679)
        prq_code = None
        if isinstance(metadata, dict) and metadata.get("invoice_id"):
            prq_code = str(metadata["invoice_id"])
        else:
            str_metadata = json.dumps(metadata) if isinstance(metadata, (dict, list)) else str(metadata)
            match = re.search(r"PRQ_[a-zA-Z0-9]+", str_metadata)
            if match:
                prq_code = match.group(0)

        payment_ref = (
            prq_code
            or event_data.get("payment_request_code")
            or event_data.get("request_code")
            or event_data.get("reference")
            or ""
        )
        tx_ref = str(event_data.get("reference") or payment_ref)
        paid_at_raw = event_data.get("paid_at") or event_data.get("paidAt")
        paid_on = datetime.fromisoformat(paid_at_raw.replace("Z", "+00:00")) if paid_at_raw else None
        amount_paid = Decimal(str(event_data.get("amount", 0))) / Decimal("100")

        return CollectionEventData(
            transaction_reference=tx_ref,
            payment_reference=str(payment_ref),
            amount_paid=amount_paid,
            payment_status="PAID",
            paid_on=paid_on,
        )

    return None


def _extract_rejected_payment_event(raw_payload: str) -> RejectedPaymentEventData | None:
    payload = json.loads(raw_payload)
    if payload.get("eventType") != "REJECTED_PAYMENT":
        return None

    event_data = payload.get("eventData", {})
    rejection_info = event_data.get("paymentRejectionInformation", {})
    expected_amount = rejection_info.get("expectedAmount")
    return RejectedPaymentEventData(
        payment_reference=event_data.get("paymentReference", ""),
        amount=Decimal(str(event_data.get("amount", "0"))),
        rejection_reason=rejection_info.get("rejectionReason", ""),
        expected_amount=Decimal(str(expected_amount)) if expected_amount is not None else None,
    )


def _extract_transfer_event(raw_payload: str) -> TransferEventData | None:
    payload = json.loads(raw_payload)

    # 1. Monnify transfer events
    event_type = payload.get("eventType", "")
    if event_type in ("SUCCESSFUL_DISBURSEMENT", "FAILED_DISBURSEMENT", "REVERSED_DISBURSEMENT"):
        event_data = payload.get("eventData", {})
        return TransferEventData(
            reference=event_data.get("reference", ""),
            success=event_type == "SUCCESSFUL_DISBURSEMENT",
            reason=event_data.get("transactionDescription") if event_type != "SUCCESSFUL_DISBURSEMENT" else None,
        )

    # 2. Paystack transfer events
    paystack_event = payload.get("event", "")
    if paystack_event in ("transfer.success", "transfer.failed", "transfer.reversed"):
        event_data = payload.get("data", {})
        reason = event_data.get("reason") or str(event_data.get("failures") or "") if paystack_event != "transfer.success" else None
        return TransferEventData(
            reference=event_data.get("reference", "") or str(event_data.get("transfer_code", "")),
            success=paystack_event == "transfer.success",
            reason=reason,
        )

    return None


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
    event_id: UUID, session_factory: async_sessionmaker, sendbyte: SendByteClient, realtime: RealtimeService
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
            rejected = _extract_rejected_payment_event(event.raw_payload)
            if rejected is not None:
                await _process_rejected_payment_event(db, service, event_id, rejected)
                return
            await service.mark_processed(event_id, error="not a collection event")
            return

        refs = [r for r in (data.payment_reference, data.transaction_reference) if r]
        result = await db.execute(select(Contribution).where(Contribution.invoice_id.in_(refs)))
        contribution = result.scalar_one_or_none()
        if contribution is None:
            await service.mark_processed(event_id, error="no contribution matches payment reference")
            return

        if contribution.status not in (ContributionStatus.PENDING, ContributionStatus.EXPIRED):
            await service.mark_processed(
                event_id, error=f"contribution already {contribution.status.value}, skipped"
            )
            return

        # Single shared decision point for pending -> paid/flagged_for_review --
        # the reconciliation job calls this exact same method.
        notifications = NotificationService(db, sendbyte)
        contribution = await ContributionService(db).apply_payment_confirmation(
            contribution,
            data.amount_paid,
            data.paid_on,
            ActorType.WEBHOOK,
            "Monnify webhook",
            notifications,
            realtime,
        )

        await service.mark_processed(event_id)
        logger.info(
            "processed webhook event %s for contribution %s -> %s",
            event_id,
            contribution.id,
            contribution.status.value,
        )


async def _process_rejected_payment_event(
    db: AsyncSession, service: WebhookService, event_id: UUID, data: RejectedPaymentEventData
) -> None:
    """Monnify rejected and reversed a transfer that didn't match the
    invoice's exact amount -- no money was actually received, so this
    never transitions a Contribution's status (it's still PENDING/EXPIRED
    exactly as it was, and the member can regenerate/retry). This only
    records *why* the transfer didn't register, instead of the event
    disappearing into the generic "not a collection event" bucket."""
    result = await db.execute(select(Contribution).where(Contribution.invoice_id == data.payment_reference))
    contribution = result.scalar_one_or_none()

    if contribution is None:
        await service.mark_processed(
            event_id, error=f"payment rejected by Monnify ({data.rejection_reason}); no matching contribution"
        )
        return

    await service.mark_processed(
        event_id,
        error=(
            f"payment rejected by Monnify: {data.rejection_reason} "
            f"(sent {data.amount}, expected {data.expected_amount}) -- "
            f"contribution {contribution.id} left as {contribution.status.value}, member can retry"
        ),
    )
    logger.info(
        "webhook event %s: Monnify rejected a mismatched payment for contribution %s (%s)",
        event_id,
        contribution.id,
        data.rejection_reason,
    )


async def process_settlement_webhook_event(
    event_id: UUID,
    session_factory: async_sessionmaker[AsyncSession],
    payload: dict,
) -> None:
    """Processes a Monnify SUCCESSFUL_SETTLEMENT or SETTLEMENT_COMPLETED event."""
    async with session_factory() as db:
        service = WebhookService(db)
        event = await db.get(WebhookEvent, event_id)
        if event is None:
            return

        try:
            if payload.get("event") == "settlement.create":
                data = payload.get("data", {})
                settlement_ref = f"paystack-{data.get('id', str(event_id))}"
                sub_account = data.get("subaccount", {})
                sub_account_code = sub_account.get("subaccount_code", "") if isinstance(sub_account, dict) else ""
                amount = float(data.get("amount") or 0.0) / 100.0
                fee = 0.0
                settled_amount = amount
                settlement_time_str = data.get("settlement_date")
                settlement_time = parse_monnify_datetime(settlement_time_str) if settlement_time_str else None
                dest_name = None
                dest_number = None
                dest_bank_code = None
                dest_bank_name = None
            else:
                event_data = payload.get("eventData", {})
                settlement_ref = (
                    event_data.get("settlementReference")
                    or event_data.get("reference")
                    or str(event_id)
                )
                sub_account_code = event_data.get("subAccountCode", "")
                amount = float(event_data.get("amount") or 0.0)
                fee = float(event_data.get("fee") or 0.0)
                settled_amount = float(event_data.get("settledAmount") or (amount - fee))
                settlement_time_str = event_data.get("settlementTime") or event_data.get("completedOn")
                settlement_time = parse_monnify_datetime(settlement_time_str) if settlement_time_str else None
                dest_name = event_data.get("destinationAccountName")
                dest_number = event_data.get("destinationAccountNumber")
                dest_bank_code = event_data.get("destinationBankCode")
                dest_bank_name = event_data.get("destinationBankName")

            log_entry = MonnifySettlementLog(
                settlement_reference=settlement_ref,
                sub_account_code=sub_account_code,
                group_id=group_id,
                amount=amount,
                fee=fee,
                settled_amount=settled_amount,
                destination_account_name=dest_name,
                destination_account_number=dest_number,
                destination_bank_code=dest_bank_code,
                destination_bank_name=dest_bank_name,
                status=event_data.get("status", "COMPLETED"),
                settlement_time=settlement_time,
                raw_payload=json.dumps(payload),
            )
            db.add(log_entry)
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                logger.info("settlement reference %s already logged", settlement_ref)

            await service.mark_processed(event.id)
            logger.info("processed settlement webhook event %s (%s)", event_id, settlement_ref)

        except Exception as exc:
            await service.mark_processed(event.id, error=str(exc))
            logger.exception("failed processing settlement webhook event %s", event_id)

