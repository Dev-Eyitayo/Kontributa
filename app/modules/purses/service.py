from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessRuleError, ForbiddenError, NotFoundError, ValidationAppError
from app.modules.contributions.service import ContributionService
from app.modules.group_admins.models import GroupAdmin
from app.modules.members.models import Member
from app.modules.purses.models import Purse, PurseStatus
from app.modules.purses.schemas import CreatePurseRequest, UpdatePurseRequest


class PurseService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.contributions = ContributionService(db)

    async def get_by_id(self, purse_id: UUID) -> Purse:
        purse = await self.db.get(Purse, purse_id)
        if purse is None:
            raise NotFoundError("purse not found")
        return purse

    def _assert_owned_by(self, admin: GroupAdmin, purse: Purse) -> None:
        if purse.created_by_group_admin_id != admin.id:
            raise ForbiddenError("cannot manage a purse you did not create")

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
        self._assert_owned_by(admin, purse)

        if purse.status != PurseStatus.OPEN:
            raise BusinessRuleError("only an open purse can be edited", code="purse_not_open")

        if payload.deadline is not None and payload.deadline <= datetime.now(timezone.utc):
            raise ValidationAppError("deadline must be in the future", code="invalid_deadline")

        if payload.amount is not None:
            purse.amount = payload.amount
            await self.contributions.update_amount_for_pending(purse.id, payload.amount)
        if payload.deadline is not None:
            purse.deadline = payload.deadline

        await self.db.commit()
        await self.db.refresh(purse)
        return purse

    async def close(self, admin: GroupAdmin, purse_id: UUID) -> Purse:
        purse = await self.get_by_id(purse_id)
        self._assert_owned_by(admin, purse)

        purse.status = PurseStatus.CLOSED
        await self.db.commit()
        await self.db.refresh(purse)
        return purse

    async def list_for_admin(self, admin: GroupAdmin, status: Optional[str] = None) -> list[Purse]:
        stmt = select(Purse).where(Purse.created_by_group_admin_id == admin.id)
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
