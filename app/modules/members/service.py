import re
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import SingleUseTokenStore
from app.core.config import settings
from app.core.exceptions import ConflictError, NotFoundError, ValidationAppError
from app.core.security import hash_password
from app.modules.auth.models import User
from app.modules.contributions.service import ContributionService
from app.modules.group_admins.models import GroupAdmin
from app.modules.invites.service import InviteService
from app.modules.members.models import Member, VerificationStatus
from app.modules.members.schemas import JoinRequest, MemberUpdateRequest
from app.modules.notifications.service import NotificationService
from app.modules.organizations.models import Group, Organization


class MemberService:
    def __init__(
        self,
        db: AsyncSession,
        verify_email_tokens: Optional[SingleUseTokenStore] = None,
        notifications: Optional[NotificationService] = None,
    ):
        self.db = db
        self.invites = InviteService(db)
        self.contributions = ContributionService(db)
        self.verify_email_tokens = verify_email_tokens
        self.notifications = notifications

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
            is_new_user = False
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
            is_new_user = True

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

        if is_new_user and self.verify_email_tokens is not None and self.notifications is not None:
            # Mirrors AuthService.register() -- join() creates a base User
            # too (per api-spec.md: "creates the base user (if new)"), so it
            # must trigger the same email-verification step, otherwise a
            # member who joined via invite could never satisfy the
            # "blocked from paying until verified" business rule.
            verify_token = await self.verify_email_tokens.issue(user.id)
            await self.notifications.send(
                to_email=user.email,
                to_name=f"{user.first_name} {user.last_name}",
                template_name="verify_email.html",
                subject="Verify your Kontributa account",
                context={
                    "first_name": user.first_name,
                    "verification_token": verify_token,
                    "expires_in_hours": settings.EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS,
                },
            )

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
