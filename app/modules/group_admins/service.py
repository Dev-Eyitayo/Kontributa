import re
import secrets
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, NotFoundError
from app.modules.auth.models import User
from app.modules.group_admins.models import GroupAdmin
from app.modules.group_admins.schemas import OnboardGroupAdminRequest
from app.modules.invites.models import InviteLink
from app.modules.invites.service import InviteService
from app.modules.members.models import Member
from app.modules.organizations.models import Group, Organization
from app.modules.purses.models import Purse


def _slugify(name: str) -> str:
    slug = re.sub(r"[^A-Z0-9]+", "", name.upper())
    return (slug or "GROUP")[:20]


class GroupAdminService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.invites = InviteService(db)

    async def get_by_user_id(self, user_id: UUID) -> GroupAdmin:
        """Returns *a* GroupAdmin row for this user -- safe only where the
        caller genuinely doesn't care which group (e.g. checking "does this
        user administer anything at all"). Every group-scoped read or write
        must go through get_admin_for_group instead, now that one admin can
        manage more than one group."""
        result = await self.db.execute(
            select(GroupAdmin).where(GroupAdmin.user_id == user_id, GroupAdmin.is_active_admin.is_(True))
        )
        admin = result.scalars().first()
        if admin is None:
            raise NotFoundError("group admin profile not found; complete onboarding first")
        return admin

    async def get_admin_for_group(self, user_id: UUID, group_id: UUID) -> GroupAdmin:
        """The authorization primitive for every group-scoped endpoint: a
        user may administer several groups, so "is this a group admin" is
        never enough on its own -- every request must prove it against the
        *specific* group_id being read or changed."""
        result = await self.db.execute(
            select(GroupAdmin).where(
                GroupAdmin.user_id == user_id,
                GroupAdmin.group_id == group_id,
                GroupAdmin.is_active_admin.is_(True),
            )
        )
        admin = result.scalar_one_or_none()
        if admin is None:
            raise ForbiddenError("you do not administer this group")
        return admin

    async def _unique_short_code(self, organization_id: UUID, requested: Optional[str], name: str) -> str:
        base = _slugify(requested) if requested else _slugify(name)
        candidate = base
        for _ in range(20):
            existing = await self.db.execute(
                select(Group).where(Group.organization_id == organization_id, Group.short_code == candidate)
            )
            if existing.scalar_one_or_none() is None:
                return candidate
            candidate = f"{base[:16]}-{secrets.token_hex(2).upper()}"
        raise ForbiddenError("could not generate a unique group code -- try a different name")

    async def onboard(self, user_id: UUID, payload: OnboardGroupAdminRequest) -> tuple[GroupAdmin, Group]:
        """Always creates a brand-new Group and makes the requesting user
        its first admin -- there is no path here that grants control of an
        existing group (see OnboardGroupAdminRequest). Callable any number
        of times: an existing admin creating a second, third, etc. group
        goes through this exact same method."""
        org = await self.db.get(Organization, payload.organization_id)
        if org is None:
            raise NotFoundError("organization not found")

        short_code = await self._unique_short_code(org.id, payload.new_group_short_code, payload.new_group_name)
        group = Group(organization_id=org.id, name=payload.new_group_name, short_code=short_code)
        self.db.add(group)
        await self.db.flush()

        admin = GroupAdmin(user_id=user_id, group_id=group.id, cohort=payload.cohort)
        self.db.add(admin)
        await self.db.commit()
        await self.db.refresh(admin)
        await self.db.refresh(group)
        return admin, group

    async def list_my_groups(self, user_id: UUID) -> list[dict]:
        result = await self.db.execute(
            select(GroupAdmin, Group, Organization)
            .join(Group, GroupAdmin.group_id == Group.id)
            .join(Organization, Group.organization_id == Organization.id)
            .where(GroupAdmin.user_id == user_id, GroupAdmin.is_active_admin.is_(True))
            .order_by(GroupAdmin.created_at)
        )
        rows = result.all()

        out = []
        for admin, group, org in rows:
            members_count = (
                await self.db.execute(select(func.count()).select_from(Member).where(Member.group_id == group.id))
            ).scalar_one()
            purses_count = (
                await self.db.execute(select(func.count()).select_from(Purse).where(Purse.group_id == group.id))
            ).scalar_one()
            out.append(
                {
                    "id": group.id,
                    "name": group.name,
                    "short_code": group.short_code,
                    "organization_id": org.id,
                    "organization_name": org.name,
                    "cohort": admin.cohort,
                    "members_count": members_count,
                    "purses_count": purses_count,
                }
            )
        return out

    async def get_me(self, user_id: UUID, group_id: UUID) -> tuple[GroupAdmin, User, Group, int, int]:
        admin = await self.get_admin_for_group(user_id, group_id)
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

    async def create_invite_link(self, user_id: UUID, group_id: UUID, payload) -> InviteLink:
        admin = await self.get_admin_for_group(user_id, group_id)
        return await self.invites.create(admin.id, admin.group_id, payload)

    async def list_invite_links(
        self, user_id: UUID, group_id: UUID, limit: int, offset: int
    ) -> tuple[list[InviteLink], int]:
        admin = await self.get_admin_for_group(user_id, group_id)
        return await self.invites.list_for_admin(admin.id, limit, offset)

    async def revoke_invite_link(self, user_id: UUID, group_id: UUID, invite_id: UUID) -> InviteLink:
        admin = await self.get_admin_for_group(user_id, group_id)
        return await self.invites.revoke(admin.id, invite_id)

    async def list_members(
        self,
        user_id: UUID,
        group_id: UUID,
        cohort: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[tuple[Member, User, Optional[InviteLink]]], int]:
        admin = await self.get_admin_for_group(user_id, group_id)


        stmt = (
            select(Member, User, InviteLink)
            .join(User, Member.user_id == User.id)
            .outerjoin(InviteLink, Member.invite_source == InviteLink.id)
            .where(Member.group_id == admin.group_id)
        )
        if admin.cohort is not None:
            stmt = stmt.where(Member.cohort == admin.cohort)
        if cohort is not None:
            stmt = stmt.where(Member.cohort == cohort)

        total = (await self.db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        result = await self.db.execute(stmt.order_by(Member.created_at).limit(limit).offset(offset))
        return [(row[0], row[1], row[2]) for row in result.all()], total
