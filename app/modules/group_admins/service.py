import re
import secrets
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.modules.auth.models import User
from app.modules.group_admins.models import GroupAdmin
from app.modules.group_admins.schemas import OnboardGroupAdminRequest
from app.modules.invites.models import InviteLink
from app.modules.invites.service import InviteService
from app.modules.members.models import Member
from app.modules.organizations.models import Group
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

    async def _unique_short_code(
        self, organization_id: Optional[UUID], requested: Optional[str], name: str
    ) -> str:
        # organization_id is None for every group created via a Group
        # Admin's own onboarding now -- NULL rows are their own scope
        # here (Group.organization_id == None compiles to IS NULL), so
        # short_code just needs to be unique among all other orgless
        # groups, matching the DB's own uq_group_org_short_code
        # constraint semantics for NULL.
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
        goes through this exact same method. Never associates the group
        with an Organization -- that's a Platform-Admin-only concept from
        this side now (see Group.organization_id)."""
        short_code = await self._unique_short_code(None, payload.new_group_short_code, payload.new_group_name)
        group = Group(organization_id=None, name=payload.new_group_name, short_code=short_code)
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
            select(GroupAdmin, Group)
            .join(Group, GroupAdmin.group_id == Group.id)
            .where(GroupAdmin.user_id == user_id, GroupAdmin.is_active_admin.is_(True))
            .order_by(GroupAdmin.created_at)
        )
        rows = result.all()
        group_ids = [group.id for _, group in rows]

        members_counts: dict[UUID, int] = {}
        purses_counts: dict[UUID, int] = {}
        if group_ids:
            member_rows = await self.db.execute(
                select(Member.group_id, func.count())
                .where(Member.group_id.in_(group_ids))
                .group_by(Member.group_id)
            )
            members_counts = dict(member_rows.all())
            purse_rows = await self.db.execute(
                select(Purse.group_id, func.count())
                .where(Purse.group_id.in_(group_ids))
                .group_by(Purse.group_id)
            )
            purses_counts = dict(purse_rows.all())

        out = []
        for admin, group in rows:
            out.append(
                {
                    "id": group.id,
                    "name": group.name,
                    "short_code": group.short_code,
                    "cohort": admin.cohort,
                    "members_count": members_counts.get(group.id, 0),
                    "purses_count": purses_counts.get(group.id, 0),
                }
            )
        return out

    async def get_me(self, user_id: UUID, group_id: UUID) -> tuple[GroupAdmin, User, Group, int, int, int]:
        admin = await self.get_admin_for_group(user_id, group_id)
        user = await self.db.get(User, admin.user_id)
        group = await self.db.get(Group, admin.group_id)
        if user is None or group is None:
            raise NotFoundError("group not found")

        members_count_result = await self.db.execute(
            select(func.count()).select_from(Member).where(Member.group_id == admin.group_id)
        )
        members_count = members_count_result.scalar_one()

        # A real count query, not a client-side filter over some capped,
        # oldest-first member list -- that undercounts (or misses entirely)
        # once a group has more members than the list's own page size, since
        # the newest joiners -- the ones this stat actually cares about --
        # are exactly the ones sorted past the cutoff.
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        new_members_result = await self.db.execute(
            select(func.count())
            .select_from(Member)
            .where(Member.group_id == admin.group_id, Member.created_at >= month_start)
        )
        new_members_this_month = new_members_result.scalar_one()

        purses_count_result = await self.db.execute(
            select(func.count()).select_from(Purse).where(Purse.group_id == admin.group_id)
        )
        purses_count = purses_count_result.scalar_one()

        return admin, user, group, members_count, purses_count, new_members_this_month

    async def update_me(self, user_id: UUID, group_id: UUID, payload) -> tuple[GroupAdmin, User]:
        """first_name/last_name only -- organization, group, and role are
        never editable through this endpoint."""
        admin = await self.get_admin_for_group(user_id, group_id)
        user = await self.db.get(User, admin.user_id)
        if user is None:
            raise NotFoundError("group admin profile not found")

        if payload.first_name is not None:
            user.first_name = payload.first_name
        if payload.last_name is not None:
            user.last_name = payload.last_name

        await self.db.commit()
        await self.db.refresh(admin)
        await self.db.refresh(user)
        return admin, user

    async def update_group(self, user_id: UUID, group_id: UUID, payload) -> Group:
        """Only an existing active admin of this exact group. Changing
        cohort here is NOT retroactive -- already-joined members and
        already-created purses keep whatever cohort they had at the time;
        only members who join, and purses created, after this call inherit
        the new value (see Group.cohort)."""
        await self.get_admin_for_group(user_id, group_id)
        group = await self.db.get(Group, group_id)
        if group is None:
            raise NotFoundError("group not found")

        if payload.name is not None:
            group.name = payload.name
        if payload.short_code is not None:
            group.short_code = payload.short_code
        if payload.cohort is not None:
            group.cohort = payload.cohort

        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            raise ConflictError(
                "another group in this organization already uses that short code", code="duplicate_short_code"
            )
        await self.db.refresh(group)
        return group

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
