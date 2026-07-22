import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import BusinessRuleError, ForbiddenError, GoneError, NotFoundError
from app.modules.invites.models import InviteLink
from app.modules.invites.schemas import InviteLinkCreateRequest
from app.modules.organizations.models import Group, Organization
from app.modules.purses.models import Purse, PurseStatus


class InviteService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _get_active(self, token: str) -> InviteLink:
        result = await self.db.execute(select(InviteLink).where(InviteLink.token == token))
        invite = result.scalar_one_or_none()
        if invite is None:
            raise NotFoundError("invite not found")
        return invite

    def _is_exhausted(self, invite: InviteLink) -> bool:
        now = datetime.now(timezone.utc)
        expired = invite.expires_at < now or invite.revoked_at is not None
        maxed_out = invite.max_uses is not None and invite.used_count >= invite.max_uses
        return expired or maxed_out

    async def resolve(self, token: str) -> tuple[InviteLink, Group, Organization, Optional[str]]:
        invite = await self._get_active(token)
        if self._is_exhausted(invite):
            raise GoneError("invite link has expired or been fully used", code="invite_exhausted")

        group = await self.db.get(Group, invite.group_id)
        if group is None:
            raise NotFoundError("group not found")
        organization = await self.db.get(Organization, group.organization_id)
        if organization is None:
            raise NotFoundError("organization not found")

        purse_title = None
        if invite.purse_id is not None:
            purse = await self.db.get(Purse, invite.purse_id)
            if purse is not None:
                purse_title = purse.title

        return invite, group, organization, purse_title

    async def redeem(self, token: str) -> InviteLink:
        invite = await self._get_active(token)
        if self._is_exhausted(invite):
            raise GoneError("invite link has expired or been fully used", code="invite_exhausted")

        invite.used_count += 1
        await self.db.commit()
        await self.db.refresh(invite)
        return invite

    async def create(
        self, group_admin_id: UUID, group_id: UUID, payload: InviteLinkCreateRequest
    ) -> InviteLink:
        if payload.purse_id is not None:
            purse = await self.db.get(Purse, payload.purse_id)
            if purse is None:
                raise NotFoundError("purse not found")
            if purse.group_id != group_id:
                raise BusinessRuleError("purse does not belong to your group", code="purse_group_mismatch")
            if purse.status != PurseStatus.OPEN:
                raise BusinessRuleError("cannot generate an invite link for a closed purse", code="purse_not_open")

        token = secrets.token_urlsafe(24)
        expires_at = datetime.now(timezone.utc) + timedelta(days=payload.expires_in_days)

        invite = InviteLink(
            token=token,
            group_id=group_id,
            cohort=payload.cohort,
            purse_id=payload.purse_id,
            created_by_group_admin_id=group_admin_id,
            expires_at=expires_at,
            max_uses=payload.max_uses,
        )
        self.db.add(invite)
        await self.db.commit()
        await self.db.refresh(invite)
        return invite

    async def list_for_admin(self, group_admin_id: UUID) -> list[InviteLink]:
        result = await self.db.execute(
            select(InviteLink)
            .where(InviteLink.created_by_group_admin_id == group_admin_id)
            .order_by(InviteLink.created_at.desc())
        )
        return list(result.scalars().all())

    async def revoke(self, group_admin_id: UUID, invite_id: UUID) -> InviteLink:
        invite = await self.db.get(InviteLink, invite_id)
        if invite is None:
            raise NotFoundError("invite not found")
        if invite.created_by_group_admin_id != group_admin_id:
            raise ForbiddenError("cannot revoke another admin's invite link")

        invite.revoked_at = datetime.now(timezone.utc)
        await self.db.commit()
        await self.db.refresh(invite)
        return invite

    @staticmethod
    def build_url(token: str) -> str:
        return f"{settings.APP_BASE_URL}/invites/{token}"

    @staticmethod
    def is_active(invite: InviteLink) -> bool:
        now = datetime.now(timezone.utc)
        if invite.revoked_at is not None or invite.expires_at < now:
            return False
        if invite.max_uses is not None and invite.used_count >= invite.max_uses:
            return False
        return True
