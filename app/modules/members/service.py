import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, ValidationAppError
from app.core.security import hash_password
from app.modules.auth.models import User
from app.modules.contributions.service import ContributionService
from app.modules.group_admins.models import GroupAdmin
from app.modules.invites.service import InviteService
from app.modules.members.models import Member, VerificationStatus
from app.modules.members.schemas import JoinRequest, MemberUpdateRequest
from app.modules.organizations.models import Group, Organization


class MemberService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.invites = InviteService(db)
        self.contributions = ContributionService(db)

    async def _get_by_user_email(self, email: str) -> User | None:
        result = await self.db.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    async def get_by_user_id(self, user_id: UUID) -> Member:
        result = await self.db.execute(select(Member).where(Member.user_id == user_id))
        member = result.scalar_one_or_none()
        if member is None:
            raise NotFoundError("member profile not found")
        return member

    async def _validate_member_id_number(self, group_id: UUID, member_id_number: str | None) -> None:
        if member_id_number is None:
            return
        group = await self.db.get(Group, group_id)
        if group is None:
            raise NotFoundError("group not found")
        organization = await self.db.get(Organization, group.organization_id)
        if organization is None or not organization.member_id_format:
            return
        if not re.match(organization.member_id_format, member_id_number):
            raise ValidationAppError(
                "member_id_number does not match the organization's configured format",
                code="member_id_format_mismatch",
                details={"expected_format": organization.member_id_format},
            )

    async def join(self, token: str, payload: JoinRequest) -> Member:
        invite, group, _organization, _purse_title = await self.invites.resolve(token)

        await self._validate_member_id_number(group.id, payload.member_id_number)

        existing_user = await self._get_by_user_email(payload.email)
        if existing_user is not None:
            existing_admin = await self.db.execute(
                select(GroupAdmin).where(GroupAdmin.user_id == existing_user.id)
            )
            if existing_admin.scalar_one_or_none() is not None:
                raise ConflictError("email already belongs to a group admin account", code="duplicate_email")

            existing_member = await self.db.execute(select(Member).where(Member.user_id == existing_user.id))
            if existing_member.scalar_one_or_none() is not None:
                raise ConflictError("email already registered as a member", code="duplicate_email")

            user = existing_user
        else:
            user = User(
                email=payload.email,
                password_hash=hash_password(payload.password),
                first_name=payload.first_name,
                last_name=payload.last_name,
                role="member",
            )
            self.db.add(user)
            await self.db.flush()

        member = Member(
            user_id=user.id,
            group_id=invite.group_id,
            cohort=invite.cohort,
            member_id_number=payload.member_id_number,
            verification_status=VerificationStatus.PENDING,
            invite_source=invite.id,
        )
        self.db.add(member)
        await self.db.commit()
        await self.db.refresh(member)

        await self.invites.redeem(token)
        await self.contributions.generate_for_new_member(member)
        await self.contributions.ensure_for_invited_purse(member, invite.purse_id)
        return member

    async def get_me(self, user_id: UUID) -> tuple[Member, User, Group]:
        member = await self.get_by_user_id(user_id)
        user = await self.db.get(User, member.user_id)
        group = await self.db.get(Group, member.group_id)
        if user is None or group is None:
            raise NotFoundError("member profile not found")
        return member, user, group

    async def update_me(self, user_id: UUID, payload: MemberUpdateRequest) -> tuple[Member, User]:
        member = await self.get_by_user_id(user_id)
        user = await self.db.get(User, member.user_id)
        if user is None:
            raise NotFoundError("member profile not found")

        if payload.member_id_number is not None:
            await self._validate_member_id_number(member.group_id, payload.member_id_number)
            member.member_id_number = payload.member_id_number
        if payload.first_name is not None:
            user.first_name = payload.first_name
        if payload.last_name is not None:
            user.last_name = payload.last_name

        await self.db.commit()
        await self.db.refresh(member)
        await self.db.refresh(user)
        return member, user
