from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import (
    AccessTokenBlacklist,
    RefreshTokenService,
    SessionActivityService,
    SingleUseTokenStore,
    create_access_token,
)
from app.core.config import settings
from app.core.exceptions import AuthError, ConflictError, ForbiddenError, NotFoundError
from app.core.security import hash_password, verify_password
from app.modules.auth.models import User
from app.modules.group_admins.models import GroupAdmin
from app.modules.members.models import Member
from app.modules.organizations.models import Group
from app.modules.auth.schemas import (
    LoginRequest,
    RefreshTokenRequest,
    RegisterRequest,
    ResetPasswordRequest,
)
from app.modules.notifications.service import NotificationService


class AuthService:
    def __init__(
        self,
        db: AsyncSession,
        refresh_tokens: RefreshTokenService,
        blacklist: AccessTokenBlacklist,
        verify_email_tokens: SingleUseTokenStore,
        reset_password_tokens: SingleUseTokenStore,
        notifications: NotificationService,
        activity: SessionActivityService,
    ):
        self.db = db
        self.refresh_tokens = refresh_tokens
        self.blacklist = blacklist
        self.verify_email_tokens = verify_email_tokens
        self.reset_password_tokens = reset_password_tokens
        self.notifications = notifications
        self.activity = activity

    async def _get_by_email(self, email: str) -> User | None:
        result = await self.db.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    async def get_me(self, user_id: UUID) -> tuple[User, bool, bool]:
        user = await self.db.get(User, user_id)
        if user is None:
            raise NotFoundError("user not found")

        # Derived, not read off User.role -- an account can hold an active
        # GroupAdmin row for some groups and an active Member row for
        # others at the same time now, and User.role only ever reflects
        # whichever one existed at registration. The frontend's unified
        # group switcher (see list_my_groups below) is driven entirely by
        # these live rows, not by role.
        has_admin_identity = (
            await self.db.execute(
                select(GroupAdmin.id).where(GroupAdmin.user_id == user_id, GroupAdmin.is_active_admin.is_(True)).limit(1)
            )
        ).scalar_one_or_none() is not None
        has_member_identity = (
            await self.db.execute(
                select(Member.id).where(Member.user_id == user_id, Member.removed_at.is_(None)).limit(1)
            )
        ).scalar_one_or_none() is not None

        return user, has_admin_identity, has_member_identity

    async def list_my_groups(self, user_id: UUID) -> list[dict]:
        """Every group this account is associated with, admin or member,
        combined into a single list -- the frontend's unified switcher
        (one list, one selection, one thing to keep in sync) replaces what
        used to be two separate role-scoped lists plus a standalone mode
        switcher."""
        admin_rows = await self.db.execute(
            select(Group)
            .join(GroupAdmin, GroupAdmin.group_id == Group.id)
            .where(GroupAdmin.user_id == user_id, GroupAdmin.is_active_admin.is_(True))
        )
        member_rows = await self.db.execute(
            select(Group)
            .join(Member, Member.group_id == Group.id)
            .where(Member.user_id == user_id, Member.removed_at.is_(None))
        )
        return [
            {"group_id": group.id, "group_name": group.name, "short_code": group.short_code, "role": "admin"}
            for group in admin_rows.scalars().all()
        ] + [
            {"group_id": group.id, "group_name": group.name, "short_code": group.short_code, "role": "member"}
            for group in member_rows.scalars().all()
        ]

    async def register(self, payload: RegisterRequest) -> User:
        existing = await self._get_by_email(payload.email)
        if existing is not None:
            raise ConflictError("an account with this email already exists", code="duplicate_email")

        user = User(
            email=payload.email,
            password_hash=hash_password(payload.password),
            first_name=payload.first_name,
            last_name=payload.last_name,
            role=payload.role,
        )
        self.db.add(user)
        try:
            await self.db.commit()
        except IntegrityError:
            # The pre-check above is a plain read, not a lock -- two
            # near-simultaneous registrations for the same email can both
            # pass it before either commits. users.email has a DB-level
            # unique constraint as the real guarantee; this turns that
            # constraint violation into the same clean 409 the normal
            # (non-racing) path already returns, instead of an unhandled
            # IntegrityError bubbling up as a 500.
            await self.db.rollback()
            raise ConflictError("an account with this email already exists", code="duplicate_email")
        await self.db.refresh(user)

        token = await self.verify_email_tokens.issue(user.id)
        await self.notifications.send(
            to_email=user.email,
            to_name=f"{user.first_name} {user.last_name}",
            template_name="verify_email.html",
            subject="Verify your Kontributa account",
            context={
                "first_name": user.first_name,
                "verification_token": token,
                "expires_in_minutes": settings.EMAIL_VERIFICATION_TOKEN_EXPIRE_MINUTES,
            },
        )
        return user

    async def resend_verification(self, email: str) -> None:
        """Mirrors forgot_password's enumeration-safe shape: silently no-ops
        for an unknown email OR an already-verified one, rather than
        revealing which case applies. The original register() token isn't
        invalidated -- SingleUseTokenStore.issue() just hands out a fresh
        one; either token still works until whichever is used first (or
        both expire)."""
        user = await self._get_by_email(email)
        if user is None or user.is_verified:
            return

        token = await self.verify_email_tokens.issue(user.id)
        await self.notifications.send(
            to_email=user.email,
            to_name=f"{user.first_name} {user.last_name}",
            template_name="verify_email.html",
            subject="Verify your Kontributa account",
            context={
                "first_name": user.first_name,
                "verification_token": token,
                "expires_in_minutes": settings.EMAIL_VERIFICATION_TOKEN_EXPIRE_MINUTES,
            },
        )

    async def verify_email(self, email: str, token: str) -> bool:
        # Peek rather than consume up front: a code presented with the
        # wrong email shouldn't burn a code that's still legitimately
        # usable by its real owner (e.g. a simple typo in the email field).
        # It's only actually consumed once the email check below passes.
        user_id = await self.verify_email_tokens.peek(token)
        if user_id is None:
            raise AuthError("invalid or expired verification token", code="token_invalid")

        user = await self.db.get(User, user_id)
        # Same error for "no such user" and "right code, wrong email" --
        # codes are looked up globally (not scoped per-email in redis), so
        # this cross-check is what actually stops someone from consuming a
        # code that was issued to a different account. Folding both cases
        # into one generic message avoids confirming a code is valid but
        # tied to some other address.
        if user is None or user.email != email:
            raise AuthError("invalid or expired verification token", code="token_invalid")

        await self.verify_email_tokens.delete(token)
        user.is_verified = True
        await self.db.commit()
        return True

    async def login(self, payload: LoginRequest) -> tuple[str, str, str]:
        user = await self._get_by_email(payload.email)
        if user is None or not verify_password(payload.password, user.password_hash):
            raise AuthError("invalid email or password", code="invalid_credentials")

        if not user.is_verified:
            raise ForbiddenError(
                "verify your email before logging in", code="email_not_verified"
            )

        refresh_token, family_id = await self.refresh_tokens.issue(user.id)
        # Logging in is itself unambiguous real human activity -- seeds the
        # very first last_active_at so the inactivity check (see refresh()
        # below) has a real baseline from the start of the session, not
        # just from whenever the first heartbeat happens to land.
        await self.activity.touch(family_id)
        access = create_access_token(user.id, user.role.value, family_id)
        return access.token, refresh_token, user.role.value

    async def refresh(self, payload: RefreshTokenRequest) -> tuple[str, str]:
        peeked = await self.refresh_tokens.peek(payload.refresh_token)
        if peeked is None:
            raise AuthError("invalid or expired refresh token", code="refresh_invalid")
        _peeked_user_id, family_id = peeked

        # Deliberately separate from the token's own TTL: a token can be
        # well within its REFRESH_TOKEN_EXPIRE_DAYS life and still get
        # rejected here specifically because no genuine activity (see
        # SessionActivityService) has been recorded recently enough.
        if await self.activity.is_stale(family_id, settings.INACTIVITY_TIMEOUT_MINUTES):
            await self.refresh_tokens.revoke_family(family_id)
            await self.activity.clear(family_id)
            raise AuthError("session expired due to inactivity", code="session_inactive")

        new_refresh_token, user_id, family_id = await self.refresh_tokens.rotate(payload.refresh_token)

        user = await self.db.get(User, user_id)
        if user is None:
            raise NotFoundError("user not found")

        access = create_access_token(user.id, user.role.value, family_id)
        return access.token, new_refresh_token

    async def heartbeat(self, family_id: str | None) -> None:
        if family_id:
            await self.activity.touch(family_id)

    async def logout(self, refresh_token: str, access_jti: str, access_exp) -> None:
        peeked = await self.refresh_tokens.peek(refresh_token)
        await self.refresh_tokens.revoke_by_token(refresh_token)
        if peeked is not None:
            await self.activity.clear(peeked[1])
        await self.blacklist.blacklist(access_jti, access_exp)

    async def forgot_password(self, email: str) -> None:
        user = await self._get_by_email(email)
        if user is None:
            return
        token = await self.reset_password_tokens.issue(user.id)
        await self.notifications.send(
            to_email=user.email,
            to_name=f"{user.first_name} {user.last_name}",
            template_name="password_reset.html",
            subject="Reset your Kontributa password",
            context={
                "first_name": user.first_name,
                "reset_token": token,
                "expires_in_minutes": settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES,
            },
        )

    async def reset_password(self, payload: ResetPasswordRequest) -> None:
        user_id = await self.reset_password_tokens.consume(payload.token)
        if user_id is None:
            raise AuthError("invalid or expired reset token", code="token_invalid")

        user = await self.db.get(User, user_id)
        if user is None:
            raise NotFoundError("user not found")

        user.password_hash = hash_password(payload.new_password)
        await self.db.commit()
