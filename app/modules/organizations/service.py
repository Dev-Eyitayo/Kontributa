from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.modules.audit.models import AuditActorType
from app.modules.audit.service import AuditService
from app.modules.auth.models import User
from app.modules.invites.models import InviteLink
from app.modules.members.models import Member
from app.modules.members.service import MemberService
from app.modules.organizations.models import Group, Organization
from app.modules.organizations.schemas import (
    AdminCreateGroupRequest,
    AdminCreateOrganizationRequest,
    AdminUpdateGroupRequest,
    AdminUpdateMemberRequest,
    AdminUpdateOrganizationRequest,
)


class OrganizationService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.audit = AuditService(db)

    async def list_organizations(self, q: Optional[str] = None) -> list[Organization]:
        stmt = select(Organization).where(Organization.active.is_(True))
        if q:
            stmt = stmt.where(Organization.name.ilike(f"%{q}%"))
        result = await self.db.execute(stmt.order_by(Organization.name))
        return list(result.scalars().all())

    async def list_all_organizations(self) -> list[Organization]:
        result = await self.db.execute(select(Organization).order_by(Organization.name))
        return list(result.scalars().all())

    async def get_organization(self, organization_id: UUID) -> Organization:
        org = await self.db.get(Organization, organization_id)
        if org is None:
            raise NotFoundError("organization not found")
        return org

    async def list_groups(self, organization_id: UUID) -> list[Group]:
        await self.get_organization(organization_id)
        result = await self.db.execute(
            select(Group).where(Group.organization_id == organization_id).order_by(Group.name)
        )
        return list(result.scalars().all())

    async def create_organization(self, payload: AdminCreateOrganizationRequest) -> Organization:
        existing = await self.db.execute(
            select(Organization).where(Organization.short_code == payload.short_code)
        )
        if existing.scalar_one_or_none() is not None:
            raise ConflictError("short_code already in use", code="duplicate_short_code")

        org = Organization(
            name=payload.name,
            short_code=payload.short_code,
            org_type=payload.org_type,
            member_id_format=payload.member_id_format,
        )
        self.db.add(org)
        await self.db.commit()
        await self.db.refresh(org)
        return org

    async def update_organization(
        self, organization_id: UUID, payload: AdminUpdateOrganizationRequest
    ) -> Organization:
        org = await self.get_organization(organization_id)

        if payload.name is not None:
            org.name = payload.name
        if payload.short_code is not None:
            org.short_code = payload.short_code
        if payload.active is not None:
            org.active = payload.active
        if payload.member_id_format is not None:
            org.member_id_format = payload.member_id_format

        await self.db.commit()
        await self.db.refresh(org)
        return org

    async def create_group(self, payload: AdminCreateGroupRequest) -> Group:
        await self.get_organization(payload.organization_id)

        group = Group(
            organization_id=payload.organization_id,
            name=payload.name,
            short_code=payload.short_code,
        )
        self.db.add(group)
        await self.db.commit()
        await self.db.refresh(group)
        return group

    async def get_group(self, group_id: UUID) -> Group:
        group = await self.db.get(Group, group_id)
        if group is None:
            raise NotFoundError("group not found")
        return group

    async def update_group(self, group_id: UUID, payload: AdminUpdateGroupRequest, actor_id: UUID) -> Group:
        """Platform-admin edit of any group, unscoped by which admin
        manages it. Changing cohort here is NOT retroactive -- same rule
        as the group admin's own update_group (see Group.cohort):
        already-joined members and already-created purses keep whatever
        cohort they had at the time; only members who join, and purses
        created, after this call inherit the new value."""
        group = await self.get_group(group_id)

        before_state = {"name": group.name, "short_code": group.short_code, "cohort": group.cohort}
        if payload.name is not None:
            group.name = payload.name
        if payload.short_code is not None:
            group.short_code = payload.short_code
        if payload.cohort is not None:
            group.cohort = payload.cohort
        after_state = {"name": group.name, "short_code": group.short_code, "cohort": group.cohort}

        if after_state != before_state:
            await self.audit.record_event(
                entity_type="group",
                entity_id=group.id,
                action="group_edited_by_platform_admin",
                actor_type=AuditActorType.PLATFORM_ADMIN,
                actor_id=actor_id,
                before_state=before_state,
                after_state=after_state,
            )

        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            raise ConflictError(
                "another group in this organization already uses that short code", code="duplicate_short_code"
            )
        await self.db.refresh(group)
        return group

    async def list_group_members(
        self, group_id: UUID, limit: int = 20, offset: int = 0
    ) -> tuple[list[tuple[Member, User, Optional[InviteLink]]], int]:
        """Platform-admin view of a group's membership -- unlike the group
        admin's own list_members, this is never cohort-scoped and always
        reachable regardless of which admin (if any) manages the group.
        Excludes removed members, same as any other active-membership
        view would."""
        await self.get_group(group_id)

        stmt = (
            select(Member, User, InviteLink)
            .join(User, Member.user_id == User.id)
            .outerjoin(InviteLink, Member.invite_source == InviteLink.id)
            .where(Member.group_id == group_id, Member.removed_at.is_(None))
        )
        total = (await self.db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        result = await self.db.execute(stmt.order_by(Member.created_at).limit(limit).offset(offset))
        return [(row[0], row[1], row[2]) for row in result.all()], total

    async def _get_active_member(self, member_id: UUID) -> Member:
        member = await self.db.get(Member, member_id)
        if member is None or member.removed_at is not None:
            raise NotFoundError("member not found")
        return member

    async def update_member(
        self, member_id: UUID, payload: AdminUpdateMemberRequest, actor_id: UUID
    ) -> tuple[Member, User]:
        """Platform-admin edit of a member's profile on their behalf --
        name and member_id_number only. Never touches contribution or
        payment history; a member's Contribution rows are untouched by
        this call entirely."""
        member = await self._get_active_member(member_id)
        user = await self.db.get(User, member.user_id)
        if user is None:
            raise NotFoundError("member not found")

        before_state = {
            "first_name": user.first_name,
            "last_name": user.last_name,
            "member_id_number": member.member_id_number,
        }
        if payload.first_name is not None:
            user.first_name = payload.first_name
        if payload.last_name is not None:
            user.last_name = payload.last_name
        if payload.member_id_number is not None:
            await MemberService(self.db).validate_member_id_number(member.group_id, payload.member_id_number)
            member.member_id_number = payload.member_id_number
        after_state = {
            "first_name": user.first_name,
            "last_name": user.last_name,
            "member_id_number": member.member_id_number,
        }

        if after_state != before_state:
            await self.audit.record_event(
                entity_type="member",
                entity_id=member.id,
                action="member_profile_edited_by_platform_admin",
                actor_type=AuditActorType.PLATFORM_ADMIN,
                actor_id=actor_id,
                before_state=before_state,
                after_state=after_state,
            )

        await self.db.commit()
        await self.db.refresh(member)
        await self.db.refresh(user)
        return member, user

    async def remove_member(self, member_id: UUID, actor_id: UUID) -> Member:
        """Soft-delete only, same convention as InviteLink.revoked_at --
        a Member row is the FK target of every Contribution the person
        ever made, so it's never hard-deleted; their payment/contribution
        history is completely untouched by this call."""
        member = await self._get_active_member(member_id)

        member.removed_at = datetime.now(timezone.utc)
        await self.audit.record_event(
            entity_type="member",
            entity_id=member.id,
            action="member_removed_by_platform_admin",
            actor_type=AuditActorType.PLATFORM_ADMIN,
            actor_id=actor_id,
            before_state={"removed_at": None},
            after_state={"removed_at": member.removed_at.isoformat()},
        )
        await self.db.commit()
        await self.db.refresh(member)
        return member
