import logging
import uuid
from decimal import ROUND_DOWN, Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.exceptions import BusinessRuleError, ConflictError, ForbiddenError, NotFoundError
from app.modules.audit.models import AuditActorType
from app.modules.audit.service import AuditService
from app.modules.contributions.models import Contribution, ContributionStatus
from app.modules.group_admins.models import GroupAdmin
from app.modules.organizations.models import Group
from app.modules.payments.service import MonnifyClient, MonnifyError
from app.modules.payouts.models import Payout, PayoutActorType, PayoutAllocation, PayoutEvent, PayoutStatus
from app.modules.payouts.schemas import CreatePayoutRequest
from app.modules.purses.models import Purse
from app.modules.settlement.models import SettlementAccount

logger = logging.getLogger("kontributa.payouts")

_OUTSTANDING_STATUSES = (PayoutStatus.REQUESTED, PayoutStatus.APPROVED, PayoutStatus.PROCESSING, PayoutStatus.COMPLETED)

# PayoutActorType and AuditActorType share the same value strings by design.
_AUDIT_ACTOR_MAP = {
    PayoutActorType.GROUP_ADMIN: AuditActorType.GROUP_ADMIN,
    PayoutActorType.PLATFORM_ADMIN: AuditActorType.PLATFORM_ADMIN,
    PayoutActorType.WEBHOOK: AuditActorType.WEBHOOK,
}


class PayoutService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.audit = AuditService(db)

    async def _write_event(
        self,
        payout: Payout,
        from_status: PayoutStatus,
        to_status: PayoutStatus,
        actor_type: PayoutActorType,
        actor_id: Optional[UUID],
        note: Optional[str],
    ) -> None:
        self.db.add(
            PayoutEvent(
                payout_id=payout.id,
                from_status=from_status,
                to_status=to_status,
                actor_type=actor_type,
                actor_id=actor_id,
                note=note,
            )
        )
        await self.audit.record_event(
            entity_type="payout",
            entity_id=payout.id,
            action="status_transition",
            actor_type=_AUDIT_ACTOR_MAP[actor_type],
            actor_id=actor_id,
            before_state={"status": from_status.value},
            after_state={"status": to_status.value, "note": note},
        )

    async def _collected_total(self, purse_id: UUID) -> Decimal:
        result = await self.db.execute(
            select(func.coalesce(func.sum(Contribution.amount_received), 0)).where(
                Contribution.purse_id == purse_id,
                Contribution.status.in_([ContributionStatus.PAID, ContributionStatus.PAID_MANUAL]),
            )
        )
        return result.scalar_one()

    async def _purse_outstanding_payouts_total(self, purse_id: UUID) -> Decimal:
        result = await self.db.execute(
            select(func.coalesce(func.sum(Payout.amount), 0)).where(
                Payout.purse_id == purse_id, Payout.status.in_(_OUTSTANDING_STATUSES)
            )
        )
        return result.scalar_one()

    async def _purse_completed_payouts_total(self, purse_id: UUID) -> Decimal:
        result = await self.db.execute(
            select(func.coalesce(func.sum(Payout.amount), 0)).where(
                Payout.purse_id == purse_id, Payout.status == PayoutStatus.COMPLETED
            )
        )
        return result.scalar_one()

    async def _purse_allocated_from_sweeps_total(self, purse_id: UUID) -> Decimal:
        """Sum of this purse's share of any group-wide sweep payout that is
        still outstanding (i.e. not rejected/failed, which release the
        allocation back). This is what lets available-balance reflect money
        that already left via a sweep even though the Payout row itself is
        purse_id-less."""
        result = await self.db.execute(
            select(func.coalesce(func.sum(PayoutAllocation.allocated_amount), 0))
            .join(Payout, PayoutAllocation.payout_id == Payout.id)
            .where(PayoutAllocation.purse_id == purse_id, Payout.status.in_(_OUTSTANDING_STATUSES))
        )
        return result.scalar_one()

    async def get_available_balance(self, purse_id: UUID) -> dict:
        """Purse-scoped balance: collected minus payouts requested directly
        against this purse minus this purse's share of any group-wide sweep
        payout (see PayoutAllocation)."""
        collected_total = await self._collected_total(purse_id)
        outstanding = await self._purse_outstanding_payouts_total(purse_id)
        allocated_from_sweeps = await self._purse_allocated_from_sweeps_total(purse_id)
        paid_out_total = await self._purse_completed_payouts_total(purse_id)
        return {
            "purse_id": purse_id,
            "collected_total": collected_total,
            "paid_out_total": paid_out_total,
            "available_balance": collected_total - outstanding - allocated_from_sweeps,
        }

    async def _available_balance_for_group(self, group_id: UUID) -> Decimal:
        purse_ids_result = await self.db.execute(select(Purse.id).where(Purse.group_id == group_id))
        purse_ids = [row[0] for row in purse_ids_result.all()]

        collected_total = Decimal(0)
        if purse_ids:
            collected_result = await self.db.execute(
                select(func.coalesce(func.sum(Contribution.amount_received), 0)).where(
                    Contribution.purse_id.in_(purse_ids),
                    Contribution.status.in_([ContributionStatus.PAID, ContributionStatus.PAID_MANUAL]),
                )
            )
            collected_total = collected_result.scalar_one()

        outstanding_result = await self.db.execute(
            select(func.coalesce(func.sum(Payout.amount), 0)).where(
                Payout.group_id == group_id, Payout.status.in_(_OUTSTANDING_STATUSES)
            )
        )
        outstanding = outstanding_result.scalar_one()
        return collected_total - outstanding

    async def _create_sweep_allocations(self, payout: Payout, group_id: UUID, sweep_amount: Decimal) -> None:
        """Attributes a group-wide sweep back to the individual purses it
        drew from, proportional to each purse's own collected total at the
        moment of the sweep. Runs inside the same transaction as the Payout
        insert and under the same Group row lock already held by create(),
        so this is atomic with the payout itself and race-free."""
        purses_result = await self.db.execute(select(Purse).where(Purse.group_id == group_id))
        purses = list(purses_result.scalars().all())

        eligible: list[tuple[Purse, Decimal]] = []
        for purse in purses:
            collected_total = await self._collected_total(purse.id)
            outstanding = await self._purse_outstanding_payouts_total(purse.id)
            allocated = await self._purse_allocated_from_sweeps_total(purse.id)
            available = collected_total - outstanding - allocated
            if available > 0:
                eligible.append((purse, collected_total))

        total_collected = sum((collected for _, collected in eligible), Decimal("0"))
        if not eligible or total_collected <= 0:
            return

        shares: list[tuple[Purse, Decimal]] = []
        running_total = Decimal("0.00")
        for purse, collected_total in eligible:
            share = (sweep_amount * collected_total / total_collected).quantize(
                Decimal("0.01"), rounding=ROUND_DOWN
            )
            shares.append((purse, share))
            running_total += share

        # Rounding truncates each share down, so the shortfall (always >= 0
        # and < a cent per purse) is folded into the last share -- this is
        # what guarantees the allocations sum to exactly sweep_amount.
        remainder = (sweep_amount - running_total).quantize(Decimal("0.01"))
        if remainder != 0:
            last_purse, last_share = shares[-1]
            shares[-1] = (last_purse, last_share + remainder)

        for purse, share in shares:
            if share <= 0:
                continue
            self.db.add(
                PayoutAllocation(payout_id=payout.id, purse_id=purse.id, allocated_amount=share)
            )

    async def create(self, admin: GroupAdmin, payload: CreatePayoutRequest) -> Payout:
        if payload.group_id != admin.group_id:
            raise ForbiddenError("cannot request a payout for another group")

        if payload.purse_id is not None:
            # Row lock is the serialization point: a second concurrent request
            # against the same purse blocks here until this transaction commits,
            # so two simultaneous requests can never both pass the balance check.
            result = await self.db.execute(
                select(Purse).where(Purse.id == payload.purse_id).with_for_update()
            )
            purse = result.scalar_one_or_none()
            if purse is None:
                raise NotFoundError("purse not found")
            if purse.group_id != admin.group_id:
                raise ForbiddenError("purse does not belong to your group")

            available = (await self.get_available_balance(purse.id))["available_balance"]
        else:
            group_result = await self.db.execute(select(Group).where(Group.id == admin.group_id).with_for_update())
            if group_result.scalar_one_or_none() is None:
                raise NotFoundError("group not found")

            available = await self._available_balance_for_group(admin.group_id)

        if payload.amount > available:
            raise BusinessRuleError(
                "requested amount exceeds the available balance", code="insufficient_balance"
            )

        payout = Payout(
            id=uuid.uuid4(),
            group_id=admin.group_id,
            purse_id=payload.purse_id,
            amount=payload.amount,
            requested_by=admin.id,
        )
        self.db.add(payout)

        if payload.purse_id is None:
            await self._create_sweep_allocations(payout, admin.group_id, payload.amount)

        await self.db.commit()
        await self.db.refresh(payout)
        return payout

    async def get_by_id(self, payout_id: UUID) -> Payout:
        payout = await self.db.get(Payout, payout_id)
        if payout is None:
            raise NotFoundError("payout not found")
        return payout

    async def list_for_group(self, group_id: UUID, status: Optional[str] = None) -> list[Payout]:
        stmt = select(Payout).where(Payout.group_id == group_id)
        if status is not None:
            stmt = stmt.where(Payout.status == status)
        result = await self.db.execute(stmt.order_by(Payout.created_at.desc()))
        return list(result.scalars().all())

    async def list_all(self, status: Optional[str] = None) -> list[Payout]:
        stmt = select(Payout)
        if status is not None:
            stmt = stmt.where(Payout.status == status)
        result = await self.db.execute(stmt.order_by(Payout.created_at.desc()))
        return list(result.scalars().all())

    async def approve_only(self, payout: Payout, approved_by_user_id: UUID) -> Payout:
        """Marks the payout approved. Does not itself call Monnify -- that
        happens as a separate subsequent step (initiate_transfer_for_payout),
        matching "approval doesn't move money, it authorizes a later step"."""
        if payout.status != PayoutStatus.REQUESTED:
            raise ConflictError("only a requested payout can be approved", code="payout_not_requestable")

        # Re-check the balance at approval time too, in case other payouts
        # were approved/completed in between request and approval. This
        # payout itself is already REQUESTED (an outstanding status), so the
        # computed balance already has payout.amount subtracted once -- add
        # it back to get "available excluding this payout" before comparing.
        if payout.purse_id is not None:
            available = (await self.get_available_balance(payout.purse_id))["available_balance"]
        else:
            available = await self._available_balance_for_group(payout.group_id)
        available_excluding_self = available + payout.amount
        if payout.amount > available_excluding_self:
            raise BusinessRuleError(
                "approving this payout would exceed the currently available balance",
                code="insufficient_balance",
            )

        from_status = payout.status
        payout.status = PayoutStatus.APPROVED
        payout.approved_by = approved_by_user_id
        await self._write_event(
            payout, from_status, PayoutStatus.APPROVED, PayoutActorType.PLATFORM_ADMIN, approved_by_user_id, None
        )
        await self.db.commit()
        await self.db.refresh(payout)
        return payout

    async def reject(self, payout: Payout, rejected_by_user_id: UUID, reason: str) -> Payout:
        if payout.status != PayoutStatus.REQUESTED:
            raise ConflictError("only a requested payout can be rejected", code="payout_not_requestable")

        from_status = payout.status
        payout.status = PayoutStatus.REJECTED
        payout.rejection_reason = reason
        await self._write_event(
            payout, from_status, PayoutStatus.REJECTED, PayoutActorType.PLATFORM_ADMIN, rejected_by_user_id, reason
        )
        await self.db.commit()
        await self.db.refresh(payout)
        return payout

    async def mark_transfer_initiated(self, payout_id: UUID, transfer_ref: str) -> Optional[Payout]:
        payout = await self.db.get(Payout, payout_id)
        if payout is None or payout.status != PayoutStatus.APPROVED:
            return None

        from_status = payout.status
        payout.status = PayoutStatus.PROCESSING
        payout.monnify_transfer_ref = transfer_ref
        await self._write_event(
            payout,
            from_status,
            PayoutStatus.PROCESSING,
            PayoutActorType.PLATFORM_ADMIN,
            None,
            f"Monnify single transfer initiated, ref={transfer_ref}",
        )
        await self.db.commit()
        await self.db.refresh(payout)
        return payout

    async def mark_transfer_initiation_failed(self, payout_id: UUID, reason: str) -> Optional[Payout]:
        payout = await self.db.get(Payout, payout_id)
        if payout is None or payout.status != PayoutStatus.APPROVED:
            return None

        from_status = payout.status
        payout.status = PayoutStatus.FAILED
        payout.failure_reason = reason
        await self._write_event(
            payout, from_status, PayoutStatus.FAILED, PayoutActorType.PLATFORM_ADMIN, None, reason
        )
        await self.db.commit()
        await self.db.refresh(payout)
        return payout

    async def apply_transfer_confirmation(
        self, payout: Payout, success: bool, failure_reason: Optional[str]
    ) -> Optional[Payout]:
        """The single place that decides how a transfer webhook confirmation
        changes a Payout's status. A failed transfer never touches
        available balance -- it's a status/failure_reason update only, and
        the money (never having left) remains available to retry."""
        if payout.status != PayoutStatus.PROCESSING:
            return None

        from_status = payout.status
        if success:
            payout.status = PayoutStatus.COMPLETED
            note = "Monnify transfer confirmed successful"
        else:
            payout.status = PayoutStatus.FAILED
            payout.failure_reason = failure_reason
            note = f"Monnify transfer failed: {failure_reason}"

        await self._write_event(payout, from_status, payout.status, PayoutActorType.WEBHOOK, None, note)
        await self.db.commit()
        await self.db.refresh(payout)
        return payout

    async def list_history(self, payout_id: UUID) -> list[PayoutEvent]:
        result = await self.db.execute(
            select(PayoutEvent).where(PayoutEvent.payout_id == payout_id).order_by(PayoutEvent.created_at)
        )
        return list(result.scalars().all())


async def initiate_transfer_for_payout(
    payout_id: UUID, session_factory: async_sessionmaker, monnify: MonnifyClient
) -> None:
    """Runs as a background task right after POST /payouts/{id}/approve
    responds -- the transfer call is deliberately not inline in the request
    that describes itself to the caller as "approval". Takes the session
    factory explicitly for the same reason the webhook background task
    does: it must bind to whichever engine the triggering request used
    (test vs. production), not a hardcoded global."""
    async with session_factory() as db:
        service = PayoutService(db)
        payout = await db.get(Payout, payout_id)
        if payout is None or payout.status != PayoutStatus.APPROVED:
            return

        settlement_result = await db.execute(
            select(SettlementAccount).where(SettlementAccount.group_id == payout.group_id)
        )
        settlement = settlement_result.scalar_one_or_none()
        if settlement is None or not settlement.account_name_verified:
            await service.mark_transfer_initiation_failed(
                payout_id, "no verified settlement account on file for this group"
            )
            return

        reference = f"payout-{payout.id}"
        try:
            result = await monnify.initiate_single_transfer(
                reference=reference,
                amount=payout.amount,
                bank_code=settlement.bank_code,
                account_number=settlement.account_number,
                account_name=settlement.bank_name,
                narration=f"Kontributa payout {payout.id}",
            )
        except MonnifyError as exc:
            logger.warning("payout %s: transfer initiation failed: %s", payout.id, exc)
            await service.mark_transfer_initiation_failed(payout_id, str(exc))
            return

        await service.mark_transfer_initiated(payout_id, result.reference)
