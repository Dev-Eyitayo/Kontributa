from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models import User
from app.modules.contributions.models import Contribution, ContributionStatus
from app.modules.members.models import Member
from app.modules.purses.models import EnrollMode, Purse, PurseStatus


class ContributionService:
    def __init__(self, db: AsyncSession):
        self.db = db

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

        created = False
        for purse in purses:
            if purse.cohort is not None and purse.cohort != member.cohort:
                continue
            existing = await self.db.execute(
                select(Contribution).where(
                    Contribution.purse_id == purse.id, Contribution.member_id == member.id
                )
            )
            if existing.scalar_one_or_none() is not None:
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
        self, purse_id: UUID, status: Optional[ContributionStatus] = None
    ) -> list[tuple[Contribution, Member, User]]:
        stmt = (
            select(Contribution, Member, User)
            .join(Member, Contribution.member_id == Member.id)
            .join(User, Member.user_id == User.id)
            .where(Contribution.purse_id == purse_id)
        )
        if status is not None:
            stmt = stmt.where(Contribution.status == status)

        result = await self.db.execute(stmt.order_by(Contribution.created_at))
        return [(row[0], row[1], row[2]) for row in result.all()]

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
