from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessRuleError, ConflictError, ForbiddenError, NotFoundError
from app.modules.auth.models import User
from app.modules.group_admins.models import GroupAdmin
from app.modules.group_admins.schemas import OnboardGroupAdminRequest
from app.modules.invites.models import InviteLink
from app.modules.invites.service import InviteService
from app.modules.members.models import Member
from app.modules.organizations.models import Group
from app.modules.purses.models import Purse


class GroupAdminService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.invites = InviteService(db)

    async def get_by_user_id(self, user_id: UUID) -> GroupAdmin:
        result = await self.db.execute(select(GroupAdmin).where(GroupAdmin.user_id == user_id))
        admin = result.scalar_one_or_none()
        if admin is None:
            raise NotFoundError("group admin profile not found; complete onboarding first")
        return admin

    async def onboard(self, user_id: UUID, payload: OnboardGroupAdminRequest) -> GroupAdmin:
        existing = await self.db.execute(select(GroupAdmin).where(GroupAdmin.user_id == user_id))
        if existing.scalar_one_or_none() is not None:
            raise ConflictError("user already has a group admin profile", code="already_onboarded")

        group = await self.db.get(Group, payload.group_id)
        if group is None:
            raise NotFoundError("group not found")
        if group.organization_id != payload.organization_id:
            raise BusinessRuleError("group does not belong to the given organization")

        admin = GroupAdmin(user_id=user_id, group_id=payload.group_id, cohort=payload.cohort)
        self.db.add(admin)
        await self.db.commit()
        await self.db.refresh(admin)
        return admin

    async def get_me(self, user_id: UUID) -> tuple[GroupAdmin, User, Group, int, int]:
        admin = await self.get_by_user_id(user_id)
        user = await self.db.get(User, admin.user_id)
        group = await self.db.get(Group, admin.group_id)
        if user is None or group is None:
            raise NotFoundError("group not found")

        members_count_result = await self.db.execute(
            select(func.count()).select_from(Member).where(Member.group_id == admin.group_id)
        )
        members_count = members_count_result.scalar_one()

        purses_count_result = await self.db.execute(
            select(func.count()).select_from(Purse).where(Purse.group_id == admin.group_id)
        )
        purses_count = purses_count_result.scalar_one()

        return admin, user, group, members_count, purses_count

    async def create_invite_link(self, user_id: UUID, payload) -> InviteLink:
        admin = await self.get_by_user_id(user_id)
        return await self.invites.create(admin.id, admin.group_id, payload)

    async def list_invite_links(self, user_id: UUID, limit: int, offset: int) -> tuple[list[InviteLink], int]:
        admin = await self.get_by_user_id(user_id)
        return await self.invites.list_for_admin(admin.id, limit, offset)

    async def revoke_invite_link(self, user_id: UUID, invite_id: UUID) -> InviteLink:
        admin = await self.get_by_user_id(user_id)
        return await self.invites.revoke(admin.id, invite_id)

    async def list_members(
        self, user_id: UUID, cohort: Optional[str] = None, limit: int = 20, offset: int = 0
    ) -> tuple[list[tuple[Member, User]], int]:
        admin = await self.get_by_user_id(user_id)

        stmt = select(Member, User).join(User, Member.user_id == User.id).where(Member.group_id == admin.group_id)
        if admin.cohort is not None:
            stmt = stmt.where(Member.cohort == admin.cohort)
        if cohort is not None:
            stmt = stmt.where(Member.cohort == cohort)

        total = (await self.db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        result = await self.db.execute(stmt.order_by(Member.created_at).limit(limit).offset(offset))
        return [(row[0], row[1]) for row in result.all()], total
