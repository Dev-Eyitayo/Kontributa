from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select, update
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
        manages it. Changing cohort here cascades onto every active
        member immediately -- same rule as the group admin's own
        update_group (see Group.cohort and GroupAdminService.update_group).
        An already-created purse's own stored cohort is a separate,
        deliberate snapshot, untouched by this; only purses created after
        this call inherit the new value."""
        group = await self.get_group(group_id)

        before_state = {"name": group.name, "short_code": group.short_code, "cohort": group.cohort}
        if payload.name is not None:
            group.name = payload.name
        if payload.short_code is not None:
            group.short_code = payload.short_code
        if payload.cohort is not None:
            group.cohort = payload.cohort
            await self.db.execute(
                update(Member).where(Member.group_id == group_id, Member.removed_at.is_(None)).values(cohort=payload.cohort)
            )
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

    async def list_all_groups_for_admin(
        self, limit: int = 20, offset: int = 0
    ) -> tuple[list[dict], int]:
        from app.modules.group_admins.models import GroupAdmin
        from app.modules.purses.models import Purse

        total_stmt = select(func.count()).select_from(Group)
        total = (await self.db.execute(total_stmt)).scalar_one()

        stmt = select(Group).order_by(Group.created_at.desc()).limit(limit).offset(offset)
        groups = (await self.db.execute(stmt)).scalars().all()
        group_ids = [g.id for g in groups]

        admins_map: dict[UUID, dict] = {}
        members_counts: dict[UUID, int] = {}
        purses_counts: dict[UUID, int] = {}

        if group_ids:
            admin_rows = await self.db.execute(
                select(GroupAdmin, User)
                .join(User, GroupAdmin.user_id == User.id)
                .where(GroupAdmin.group_id.in_(group_ids), GroupAdmin.is_active_admin.is_(True))
            )
            for ga, user in admin_rows.all():
                admins_map[ga.group_id] = {
                    "user_id": str(user.id),
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                    "email": user.email,
                }

            member_rows = await self.db.execute(
                select(Member.group_id, func.count())
                .where(Member.group_id.in_(group_ids), Member.removed_at.is_(None))
                .group_by(Member.group_id)
            )
            members_counts = dict(member_rows.all())

            purse_rows = await self.db.execute(
                select(Purse.group_id, func.count())
                .where(Purse.group_id.in_(group_ids))
                .group_by(Purse.group_id)
            )
            purses_counts = dict(purse_rows.all())

        items = []
        for g in groups:
            items.append(
                {
                    "id": str(g.id),
                    "name": g.name,
                    "short_code": g.short_code,
                    "cohort": g.cohort,
                    "created_at": g.created_at.isoformat(),
                    "admin": admins_map.get(g.id),
                    "members_count": members_counts.get(g.id, 0),
                    "purses_count": purses_counts.get(g.id, 0),
                }
            )
        return items, total

    async def get_group_detail_for_admin(self, group_id: UUID) -> dict:
        from app.modules.group_admins.models import GroupAdmin
        from app.modules.purses.models import Purse

        group = await self.get_group(group_id)
        admin_row = (
            await self.db.execute(
                select(GroupAdmin, User)
                .join(User, GroupAdmin.user_id == User.id)
                .where(GroupAdmin.group_id == group_id, GroupAdmin.is_active_admin.is_(True))
                .limit(1)
            )
        ).first()

        admin_data = None
        if admin_row:
            ga, user = admin_row
            admin_data = {
                "user_id": str(user.id),
                "first_name": user.first_name,
                "last_name": user.last_name,
                "email": user.email,
            }

        members_count = (
            await self.db.execute(
                select(func.count()).select_from(Member).where(Member.group_id == group_id, Member.removed_at.is_(None))
            )
        ).scalar_one()

        purses_count = (
            await self.db.execute(
                select(func.count()).select_from(Purse).where(Purse.group_id == group_id)
            )
        ).scalar_one()

        from app.modules.settlement.models import SettlementAccount

        settlement_acc = (
            await self.db.execute(
                select(SettlementAccount).where(SettlementAccount.group_id == group_id)
            )
        ).scalar_one_or_none()

        settlement_data = None
        if settlement_acc:
            settlement_data = {
                "id": str(settlement_acc.id),
                "bank_name": settlement_acc.bank_name,
                "account_number": settlement_acc.account_number,
                "account_name_verified": settlement_acc.account_name_verified,
                "verified_at": settlement_acc.verified_at.isoformat() if settlement_acc.verified_at else None,
                "settlement_mode": settlement_acc.settlement_mode.value,
                "direct_sub_account_code": settlement_acc.direct_sub_account_code,
                "payment_provider": getattr(settlement_acc, "payment_provider", "monnify"),
            }

        return {
            "id": str(group.id),
            "name": group.name,
            "short_code": group.short_code,
            "cohort": group.cohort,
            "created_at": group.created_at.isoformat(),
            "admin": admin_data,
            "members_count": members_count,
            "purses_count": purses_count,
            "settlement_account": settlement_data,
        }

    async def list_settlements_for_admin(
        self, limit: int = 20, offset: int = 0
    ) -> tuple[list[dict], int]:
        from app.modules.settlement.models import MonnifySettlementLog

        total_stmt = select(func.count()).select_from(MonnifySettlementLog)
        total = (await self.db.execute(total_stmt)).scalar_one()

        stmt = (
            select(MonnifySettlementLog, Group)
            .outerjoin(Group, MonnifySettlementLog.group_id == Group.id)
            .order_by(MonnifySettlementLog.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self.db.execute(stmt)).all()

        items = []
        for log, group in rows:
            items.append(
                {
                    "id": str(log.id),
                    "settlement_reference": log.settlement_reference,
                    "sub_account_code": log.sub_account_code,
                    "group_id": str(log.group_id) if log.group_id else None,
                    "group_name": group.name if group else None,
                    "amount": str(log.amount),
                    "fee": str(log.fee),
                    "settled_amount": str(log.settled_amount),
                    "destination_account_name": log.destination_account_name,
                    "destination_account_number": log.destination_account_number,
                    "destination_bank_code": log.destination_bank_code,
                    "destination_bank_name": log.destination_bank_name,
                    "status": log.status,
                    "settlement_time": log.settlement_time.isoformat() if log.settlement_time else None,
                    "created_at": log.created_at.isoformat(),
                }
            )
        return items, total

    async def delete_group(self, group_id: UUID, actor_id: UUID) -> None:
        group = await self.db.get(Group, group_id)
        if group is None:
            raise NotFoundError("group not found")

        from app.modules.settlement.models import MonnifySettlementLog

        # Unlink group_id references in settlement logs
        await self.db.execute(update(MonnifySettlementLog).where(MonnifySettlementLog.group_id == group_id).values(group_id=None))

        # Record platform admin audit event for group deletion
        await self.audit.record_event(
            entity_type="group",
            entity_id=group_id,
            action="group_deleted",
            actor_type=AuditActorType.PLATFORM_ADMIN,
            actor_id=actor_id,
            before_state={"name": group.name, "short_code": group.short_code},
        )

        # Deleting the group entity triggers DB foreign key CASCADE to child tables automatically
        await self.db.delete(group)
        await self.db.commit()



