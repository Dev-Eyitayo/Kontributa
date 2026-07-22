from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    BusinessRuleError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationAppError,
)
from app.modules.audit.models import AuditActorType
from app.modules.audit.service import AuditService
from app.modules.contributions.models import Contribution
from app.modules.contributions.service import ContributionService
from app.modules.group_admins.models import GroupAdmin
from app.modules.members.models import Member
from app.modules.purses.models import Purse, PurseStatus
from app.modules.purses.schemas import CreatePurseRequest, UpdatePurseRequest


class PurseService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.contributions = ContributionService(db)
        self.audit = AuditService(db)

    async def get_by_id(self, purse_id: UUID) -> Purse:
        purse = await self.db.get(Purse, purse_id)
        if purse is None:
            raise NotFoundError("purse not found")
        return purse

    def _assert_group_scoped(self, admin: GroupAdmin, purse: Purse) -> None:
        """Purses belong to the group, not the admin who created them -- a
        GroupAdmin leaving never orphans a purse; any admin of the same
        group has full visibility and control over it."""
        if purse.group_id != admin.group_id:
            raise ForbiddenError("cannot manage a purse outside your group")

    async def create(self, admin: GroupAdmin, payload: CreatePurseRequest) -> Purse:
        if payload.deadline <= datetime.now(timezone.utc):
            raise ValidationAppError("deadline must be in the future", code="invalid_deadline")

        purse = Purse(
            group_id=admin.group_id,
            cohort=payload.cohort if payload.cohort is not None else admin.cohort,
            created_by_group_admin_id=admin.id,
            title=payload.title,
            amount=payload.amount,
            deadline=payload.deadline,
            enroll_mode=payload.enroll_mode,
            status=PurseStatus.OPEN,
        )
        self.db.add(purse)
        await self.db.commit()
        await self.db.refresh(purse)

        await self.contributions.generate_for_purse(purse)
        return purse

    async def update(self, admin: GroupAdmin, purse_id: UUID, payload: UpdatePurseRequest) -> Purse:
        purse = await self.get_by_id(purse_id)
        self._assert_group_scoped(admin, purse)

        if purse.status != PurseStatus.OPEN:
            raise BusinessRuleError("only an open purse can be edited", code="purse_not_open")

        if payload.deadline is not None and payload.deadline <= datetime.now(timezone.utc):
            raise ValidationAppError("deadline must be in the future", code="invalid_deadline")

        before_state = {"amount": str(purse.amount), "deadline": purse.deadline.isoformat()}

        if payload.amount is not None:
            purse.amount = payload.amount
            await self.contributions.update_amount_for_pending(purse.id, payload.amount)
        if payload.deadline is not None:
            purse.deadline = payload.deadline

        after_state = {"amount": str(purse.amount), "deadline": purse.deadline.isoformat()}
        await self.audit.record_event(
            entity_type="purse",
            entity_id=purse.id,
            action="purse_edited",
            actor_type=AuditActorType.GROUP_ADMIN,
            actor_id=admin.id,
            before_state=before_state,
            after_state=after_state,
        )

        await self.db.commit()
        await self.db.refresh(purse)
        return purse

    async def close(self, admin: GroupAdmin, purse_id: UUID) -> Purse:
        purse = await self.get_by_id(purse_id)
        self._assert_group_scoped(admin, purse)

        before_state = {"status": purse.status.value}
        purse.status = PurseStatus.CLOSED
        await self.audit.record_event(
            entity_type="purse",
            entity_id=purse.id,
            action="purse_closed",
            actor_type=AuditActorType.GROUP_ADMIN,
            actor_id=admin.id,
            before_state=before_state,
            after_state={"status": PurseStatus.CLOSED.value},
        )

        await self.db.commit()
        await self.db.refresh(purse)
        return purse

    async def add_member(self, admin: GroupAdmin, purse_id: UUID, member_id: UUID) -> Contribution:
        """Manually backfills a single existing member onto a purse they
        weren't swept into -- e.g. a snapshot purse created before they
        joined, or a cohort mismatch at generation time. The automatic paths
        (generate_for_purse at creation, generate_for_new_member on join)
        only ever run once per member per purse, so this is the only way to
        add someone after the fact without a purse-scoped invite link."""
        purse = await self.get_by_id(purse_id)
        self._assert_group_scoped(admin, purse)

        if purse.status != PurseStatus.OPEN:
            raise BusinessRuleError("only an open purse can have members added", code="purse_not_open")

        member = await self.db.get(Member, member_id)
        if member is None or member.group_id != purse.group_id:
            raise NotFoundError("member not found in this group")

        if purse.cohort is not None and member.cohort != purse.cohort:
            raise BusinessRuleError(
                "member's cohort does not match this purse's cohort", code="cohort_mismatch"
            )

        existing = await self.contributions.get_for_member(purse.id, member.id)
        if existing is not None:
            raise ConflictError("member is already enrolled in this purse", code="already_enrolled")

        contribution = Contribution(purse_id=purse.id, member_id=member.id, amount_expected=purse.amount)
        self.db.add(contribution)
        await self.db.commit()
        await self.db.refresh(contribution)

        await self.audit.record_event(
            entity_type="contribution",
            entity_id=contribution.id,
            action="member_added_to_purse",
            actor_type=AuditActorType.GROUP_ADMIN,
            actor_id=admin.id,
            before_state=None,
            after_state={"purse_id": str(purse.id), "member_id": str(member.id)},
        )

        return contribution

    async def list_for_admin(self, admin: GroupAdmin, status: Optional[str] = None) -> list[Purse]:
        stmt = select(Purse).where(Purse.group_id == admin.group_id)
        if status is not None:
            stmt = stmt.where(Purse.status == status)
        result = await self.db.execute(stmt.order_by(Purse.created_at.desc()))
        return list(result.scalars().all())

    async def list_for_member(self, member: Member, status: Optional[str] = None) -> list[tuple[Purse, str]]:
        """Purses the member is eligible for, i.e. has a Contribution row for."""
        rows = await self.contributions.list_member_purses(member.id)
        result = [(purse, contribution.status.value) for contribution, purse in rows]
        if status is not None:
            result = [(purse, cstatus) for purse, cstatus in result if purse.status.value == status]
        return result

    async def get_detail(self, purse_id: UUID) -> Purse:
        return await self.get_by_id(purse_id)
