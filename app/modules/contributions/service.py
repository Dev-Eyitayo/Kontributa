from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import BusinessRuleError, NotFoundError
from app.modules.audit.models import AuditActorType
from app.modules.audit.service import AuditService
from app.modules.auth.models import User
from app.modules.contributions.models import ActorType, Contribution, ContributionEvent, ContributionStatus
from app.modules.group_admins.models import GroupAdmin
from app.modules.members.models import Member
from app.modules.notifications.service import NotificationService
from app.modules.organizations.models import Group
from app.modules.payments.service import MonnifyClient
from app.modules.purses.models import EnrollMode, Purse, PurseStatus
from app.modules.settlement.models import SettlementAccount, SettlementMode

_AUDIT_ACTOR_MAP = {
    ActorType.WEBHOOK: AuditActorType.WEBHOOK,
    ActorType.RECONCILIATION_JOB: AuditActorType.RECONCILIATION_JOB,
    ActorType.REP_MANUAL: AuditActorType.GROUP_ADMIN,
}


class ContributionService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.audit = AuditService(db)

    async def generate_for_purse(self, purse: Purse) -> list[Contribution]:
        """Called once, at purse creation. Snapshots every currently-eligible
        member regardless of enroll_mode -- auto_enroll purses additionally
        pick up future joiners via generate_for_new_member."""
        stmt = select(Member).where(Member.group_id == purse.group_id)
        if purse.cohort is not None:
            stmt = stmt.where(Member.cohort == purse.cohort)

        members = (await self.db.execute(stmt)).scalars().all()
        rows = [
            Contribution(purse_id=purse.id, member_id=member.id, amount_expected=purse.amount)
            for member in members
        ]
        if rows:
            self.db.add_all(rows)
            await self.db.commit()
        return rows

    async def generate_for_new_member(self, member: Member) -> None:
        """Called when a member joins. Only auto_enroll, still-open purses in
        their group (matching cohort, if the purse has one) pick up latecomers --
        snapshot purses never retroactively include them."""
        stmt = select(Purse).where(
            Purse.group_id == member.group_id,
            Purse.enroll_mode == EnrollMode.AUTO_ENROLL,
            Purse.status == PurseStatus.OPEN,
        )
        purses = (await self.db.execute(stmt)).scalars().all()
        eligible = [p for p in purses if p.cohort is None or p.cohort == member.cohort]

        existing_purse_ids: set[UUID] = set()
        if eligible:
            existing_rows = await self.db.execute(
                select(Contribution.purse_id).where(
                    Contribution.member_id == member.id,
                    Contribution.purse_id.in_([p.id for p in eligible]),
                )
            )
            existing_purse_ids = set(existing_rows.scalars().all())

        created = False
        for purse in eligible:
            if purse.id in existing_purse_ids:
                continue
            self.db.add(Contribution(purse_id=purse.id, member_id=member.id, amount_expected=purse.amount))
            created = True

        if created:
            await self.db.commit()

    async def ensure_for_invited_purse(self, member: Member, purse_id: Optional[UUID]) -> None:
        """A purse-specific invite link grants eligibility for that exact
        purse regardless of enroll_mode -- that's the whole point of scoping
        an invite to one purse, so a snapshot purse must still pick up a
        member who joined through a link pointed at it."""
        if purse_id is None:
            return

        purse = await self.db.get(Purse, purse_id)
        if purse is None or purse.status != PurseStatus.OPEN:
            return

        existing = await self.get_for_member(purse_id, member.id)
        if existing is not None:
            return

        self.db.add(Contribution(purse_id=purse_id, member_id=member.id, amount_expected=purse.amount))
        await self.db.commit()

    async def update_amount_for_pending(self, purse_id: UUID, new_amount: Decimal) -> None:
        """Already-paid (or otherwise resolved) contributions keep their
        original amount_expected -- only still-pending ones inherit the edit."""
        stmt = select(Contribution).where(
            Contribution.purse_id == purse_id, Contribution.status == ContributionStatus.PENDING
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        for contribution in rows:
            contribution.amount_expected = new_amount
        if rows:
            await self.db.commit()

    async def list_for_purse(
        self,
        purse_id: UUID,
        status: Optional[ContributionStatus] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[tuple[Contribution, Member, User]], int]:
        stmt = (
            select(Contribution, Member, User)
            .join(Member, Contribution.member_id == Member.id)
            .join(User, Member.user_id == User.id)
            .where(Contribution.purse_id == purse_id)
        )
        if status is not None:
            stmt = stmt.where(Contribution.status == status)

        total = (await self.db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        result = await self.db.execute(stmt.order_by(Contribution.created_at).limit(limit).offset(offset))
        return [(row[0], row[1], row[2]) for row in result.all()], total

    async def summary_for_purse(self, purse_id: UUID) -> dict:
        stmt = select(Contribution.status, func.count(), func.coalesce(func.sum(Contribution.amount_received), 0)).where(
            Contribution.purse_id == purse_id
        ).group_by(Contribution.status)
        rows = (await self.db.execute(stmt)).all()

        counts = {status.value: 0 for status in ContributionStatus}
        collected_by_status: dict[str, Decimal] = {}
        for status, count, collected in rows:
            counts[status.value] = count
            collected_by_status[status.value] = collected

        total_collected = collected_by_status.get(ContributionStatus.PAID.value, Decimal(0)) + collected_by_status.get(
            ContributionStatus.PAID_MANUAL.value, Decimal(0)
        )
        total_count = sum(counts.values())
        completed_count = counts[ContributionStatus.PAID.value] + counts[ContributionStatus.PAID_MANUAL.value]
        percent_complete = round((completed_count / total_count) * 100, 2) if total_count else 0.0

        return {
            "paid_count": counts[ContributionStatus.PAID.value],
            "pending_count": counts[ContributionStatus.PENDING.value],
            "expired_count": counts[ContributionStatus.EXPIRED.value],
            "flagged_count": counts[ContributionStatus.FLAGGED_FOR_REVIEW.value],
            "total_collected": total_collected,
            "percent_complete": percent_complete,
        }

    async def counts_for_purses(self, purse_ids: list[UUID]) -> dict[UUID, tuple[int, int]]:
        """Returns {purse_id: (paid_count, total_count)} for a batch of purses."""
        if not purse_ids:
            return {}
        stmt = select(Contribution.purse_id, Contribution.status, func.count()).where(
            Contribution.purse_id.in_(purse_ids)
        ).group_by(Contribution.purse_id, Contribution.status)
        rows = (await self.db.execute(stmt)).all()

        result: dict[UUID, tuple[int, int]] = {pid: (0, 0) for pid in purse_ids}
        for purse_id, status, count in rows:
            paid, total = result[purse_id]
            total += count
            if status in (ContributionStatus.PAID, ContributionStatus.PAID_MANUAL):
                paid += count
            result[purse_id] = (paid, total)
        return result

    async def collected_totals_for_purses(self, purse_ids: list[UUID]) -> dict[UUID, Decimal]:
        """Returns {purse_id: total_collected} (paid + paid_manual amount_received)
        for a batch of purses -- one query, so a purses-list screen can show a real
        collected figure per row without an N+1 fetch of each purse's own summary."""
        if not purse_ids:
            return {}
        stmt = (
            select(Contribution.purse_id, func.coalesce(func.sum(Contribution.amount_received), 0))
            .where(
                Contribution.purse_id.in_(purse_ids),
                Contribution.status.in_([ContributionStatus.PAID, ContributionStatus.PAID_MANUAL]),
            )
            .group_by(Contribution.purse_id)
        )
        rows = (await self.db.execute(stmt)).all()
        result: dict[UUID, Decimal] = {pid: Decimal(0) for pid in purse_ids}
        for purse_id, collected in rows:
            result[purse_id] = collected
        return result

    async def get_for_member(self, purse_id: UUID, member_id: UUID) -> Optional[Contribution]:
        result = await self.db.execute(
            select(Contribution).where(Contribution.purse_id == purse_id, Contribution.member_id == member_id)
        )
        return result.scalar_one_or_none()

    async def list_member_purses(self, member_id: UUID) -> list[tuple[Contribution, Purse]]:
        stmt = (
            select(Contribution, Purse)
            .join(Purse, Contribution.purse_id == Purse.id)
            .where(Contribution.member_id == member_id)
            .order_by(Purse.deadline.asc())
        )
        result = await self.db.execute(stmt)
        return [(row[0], row[1]) for row in result.all()]

    async def list_purses_for_user(self, user_id: UUID) -> list[tuple[Contribution, Purse, Group]]:
        """Every purse across *every* group this user is a Member of --
        list_member_purses above is scoped to one Member row, which used to
        be the only kind a user could have. Joins through Member (rather
        than taking a member_id) specifically so a member in more than one
        group sees every group's purses in one response, each still
        resolvable back to which group it came from via the returned Group."""
        stmt = (
            select(Contribution, Purse, Group)
            .join(Member, Contribution.member_id == Member.id)
            .join(Purse, Contribution.purse_id == Purse.id)
            .join(Group, Purse.group_id == Group.id)
            .where(Member.user_id == user_id)
            .order_by(Purse.deadline.asc())
        )
        result = await self.db.execute(stmt)
        return [(row[0], row[1], row[2]) for row in result.all()]

    async def get_by_id(self, contribution_id: UUID) -> Contribution:
        contribution = await self.db.get(Contribution, contribution_id)
        if contribution is None:
            raise NotFoundError("contribution not found")
        return contribution

    async def _write_event(
        self,
        contribution: Contribution,
        from_status: ContributionStatus,
        to_status: ContributionStatus,
        actor_type: ActorType,
        actor_id: Optional[UUID],
        note: Optional[str],
    ) -> None:
        self.db.add(
            ContributionEvent(
                contribution_id=contribution.id,
                from_status=from_status,
                to_status=to_status,
                actor_type=actor_type,
                actor_id=actor_id,
                note=note,
            )
        )
        # Also written to the universal AuditLog, in addition to the
        # domain-specific ContributionEvent row above -- both tables are
        # append-only and neither replaces the other.
        await self.audit.record_event(
            entity_type="contribution",
            entity_id=contribution.id,
            action="status_transition",
            actor_type=_AUDIT_ACTOR_MAP[actor_type],
            actor_id=actor_id,
            before_state={"status": from_status.value},
            after_state={"status": to_status.value, "note": note},
        )

    async def _notify_member(
        self, contribution: Contribution, notifications: NotificationService, template_name: str, subject: str, context: dict
    ) -> None:
        member = await self.db.get(Member, contribution.member_id)
        user = await self.db.get(User, member.user_id) if member is not None else None
        if user is None:
            return
        await notifications.send(
            to_email=user.email,
            to_name=f"{user.first_name} {user.last_name}",
            template_name=template_name,
            subject=subject,
            context={"first_name": user.first_name, **context},
        )

    async def expire_if_needed(
        self, contribution: Contribution, notifications: Optional[NotificationService] = None
    ) -> Contribution:
        """Lazily applies the pending -> expired transition once the stored
        invoice's validity window has lapsed.

        `notifications` is optional and skipped entirely when None -- only
        callers that explicitly wire a NotificationService trigger the
        expiry-notice email; this keeps email sending opt-in per call site
        rather than a hidden side effect of calling this method."""
        if (
            contribution.status == ContributionStatus.PENDING
            and contribution.invoice_expires_at is not None
            and contribution.invoice_expires_at <= datetime.now(timezone.utc)
        ):
            await self._write_event(
                contribution,
                ContributionStatus.PENDING,
                ContributionStatus.EXPIRED,
                ActorType.RECONCILIATION_JOB,
                None,
                "invoice validity window lapsed unpaid",
            )
            contribution.status = ContributionStatus.EXPIRED
            await self.db.commit()
            await self.db.refresh(contribution)

            if notifications is not None:
                purse = await self.db.get(Purse, contribution.purse_id)
                if purse is not None:
                    await self._notify_member(
                        contribution,
                        notifications,
                        "contribution_expired.html",
                        f"Your invoice for {purse.title} expired",
                        {
                            "purse_title": purse.title,
                            "amount": str(contribution.amount_expected - contribution.amount_received),
                        },
                    )
        return contribution

    async def generate_invoice(
        self,
        contribution: Contribution,
        monnify: MonnifyClient,
        member: Member,
        member_user: User,
        purse: Purse,
        notifications: Optional[NotificationService] = None,
    ) -> Contribution:
        contribution = await self.expire_if_needed(contribution, notifications)

        now = datetime.now(timezone.utc)
        has_live_invoice = (
            contribution.status == ContributionStatus.PENDING
            and contribution.account_number is not None
            and contribution.invoice_expires_at is not None
            and contribution.invoice_expires_at > now
        )
        if has_live_invoice:
            return contribution

        if contribution.status not in (ContributionStatus.PENDING, ContributionStatus.EXPIRED):
            raise BusinessRuleError(
                "an invoice can only be generated for a pending or expired contribution",
                code="invoice_not_generatable",
            )

        # A contribution back in `pending` after a request_topup resolution has
        # already had part of amount_expected paid -- only invoice the remainder.
        outstanding = contribution.amount_expected - (contribution.amount_received or Decimal(0))
        expires_at = now + timedelta(minutes=settings.MONNIFY_INVOICE_EXPIRY_MINUTES)
        invoice_reference = f"{contribution.id}-{uuid4().hex[:8]}"

        # Direct-mode groups route their share straight to their own
        # Monnify sub-account via an income split on this specific invoice;
        # custodian-mode groups (or a direct-mode group whose sub-account
        # setup is somehow missing) get exactly today's behavior. Either
        # way, payment confirmation below is still detected off our own
        # invoice_reference -- this only changes where the money settles.
        income_split_config = None
        settlement = (
            await self.db.execute(select(SettlementAccount).where(SettlementAccount.group_id == purse.group_id))
        ).scalar_one_or_none()
        if settlement and settlement.settlement_mode == SettlementMode.DIRECT and settlement.direct_sub_account_code:
            income_split_config = [
                {
                    "subAccountCode": settlement.direct_sub_account_code,
                    "feePercentage": 100,
                    "feeBearer": False,
                }
            ]

        invoice = await monnify.create_invoice(
            invoice_reference=invoice_reference,
            amount=outstanding,
            customer_name=f"{member_user.first_name} {member_user.last_name}",
            customer_email=member_user.email,
            description=purse.title,
            expires_at=expires_at,
            income_split_config=income_split_config,
        )

        from_status = contribution.status
        contribution.invoice_id = invoice.invoice_reference
        contribution.account_number = invoice.account_number
        contribution.bank_name = invoice.bank_name
        contribution.invoice_expires_at = invoice.expires_at

        if from_status == ContributionStatus.EXPIRED:
            await self._write_event(
                contribution,
                from_status,
                ContributionStatus.PENDING,
                ActorType.RECONCILIATION_JOB,
                None,
                "fresh invoice generated after expiry",
            )
            contribution.status = ContributionStatus.PENDING

        await self.db.commit()
        await self.db.refresh(contribution)
        return contribution

    async def apply_payment_confirmation(
        self,
        contribution: Contribution,
        amount_paid: Decimal,
        paid_on: Optional[datetime],
        actor_type: ActorType,
        note_prefix: str,
        notifications: Optional[NotificationService] = None,
    ) -> Optional[Contribution]:
        """The single place that decides how a payment confirmation --
        whether from a Monnify webhook or the reconciliation job polling
        transaction status directly -- changes a Contribution's status.
        Both callers must route through here rather than each deciding
        the pending/paid/flagged transition themselves.

        Returns None (no-op) if the contribution isn't pending -- already
        resolved contributions are left alone, which is also what makes
        repeated reconciliation runs safe against double-applying."""
        if contribution.status != ContributionStatus.PENDING:
            return None

        from_status = contribution.status
        new_total_received = (contribution.amount_received or Decimal(0)) + amount_paid

        if new_total_received == contribution.amount_expected:
            contribution.status = ContributionStatus.PAID
            contribution.paid_at = paid_on or datetime.now(timezone.utc)
            note = f"{note_prefix}: amountPaid={amount_paid}, matched amount_expected"
        else:
            contribution.status = ContributionStatus.FLAGGED_FOR_REVIEW
            note = f"{note_prefix}: amountPaid={amount_paid}, expected={contribution.amount_expected}"

        contribution.amount_received = new_total_received

        await self._write_event(contribution, from_status, contribution.status, actor_type, None, note)
        await self.db.commit()
        await self.db.refresh(contribution)

        if notifications is not None and contribution.status == ContributionStatus.PAID:
            purse = await self.db.get(Purse, contribution.purse_id)
            if purse is not None:
                await self._notify_member(
                    contribution,
                    notifications,
                    "payment_receipt.html",
                    f"Payment received -- {purse.title}",
                    {
                        "purse_title": purse.title,
                        "amount": str(contribution.amount_received),
                        "paid_at": contribution.paid_at.isoformat() if contribution.paid_at else "",
                    },
                )

        return contribution

    async def mark_manual(self, contribution: Contribution, admin: GroupAdmin, amount_received: Decimal, note: str) -> Contribution:
        if contribution.status in (ContributionStatus.PAID, ContributionStatus.PAID_MANUAL):
            raise BusinessRuleError(
                "this contribution has already been resolved", code="contribution_already_resolved"
            )

        from_status = contribution.status
        contribution.amount_received = (contribution.amount_received or Decimal(0)) + amount_received
        contribution.status = ContributionStatus.PAID_MANUAL
        contribution.paid_at = datetime.now(timezone.utc)

        await self._write_event(
            contribution, from_status, ContributionStatus.PAID_MANUAL, ActorType.REP_MANUAL, admin.id, note
        )
        await self.db.commit()
        await self.db.refresh(contribution)
        return contribution

    async def resolve_flag(self, contribution: Contribution, admin: GroupAdmin, resolution: str) -> Contribution:
        if contribution.status != ContributionStatus.FLAGGED_FOR_REVIEW:
            raise BusinessRuleError(
                "only a flagged_for_review contribution can be resolved", code="contribution_not_flagged"
            )

        from_status = contribution.status
        if resolution == "accept_partial":
            contribution.status = ContributionStatus.PAID
            contribution.paid_at = datetime.now(timezone.utc)
            note = "rep accepted the received amount as final"
        elif resolution == "request_topup":
            contribution.status = ContributionStatus.PENDING
            note = "rep requested a top-up for the shortfall"
        elif resolution == "refund":
            raise BusinessRuleError(
                "refund disbursement is not available until Phase 5's Monnify transfer integration",
                code="refund_not_yet_supported",
            )
        else:
            raise BusinessRuleError("unknown resolution", code="invalid_resolution")

        await self._write_event(
            contribution, from_status, contribution.status, ActorType.REP_MANUAL, admin.id, note
        )
        await self.db.commit()
        await self.db.refresh(contribution)
        return contribution

    async def list_history(
        self, contribution_id: UUID, limit: int = 20, offset: int = 0
    ) -> tuple[list[ContributionEvent], int]:
        stmt = select(ContributionEvent).where(ContributionEvent.contribution_id == contribution_id)
        total = (await self.db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        result = await self.db.execute(stmt.order_by(ContributionEvent.created_at).limit(limit).offset(offset))
        return list(result.scalars().all()), total
