from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import (
    AccessTokenBlacklist,
    RefreshTokenService,
    SingleUseTokenStore,
    create_access_token,
)
from app.core.email import send_email
from app.core.exceptions import AuthError, ConflictError, NotFoundError
from app.core.security import hash_password, verify_password
from app.modules.auth.models import User
from app.modules.auth.schemas import (
    LoginRequest,
    RefreshTokenRequest,
    RegisterRequest,
    ResetPasswordRequest,
)


class AuthService:
    def __init__(
        self,
        db: AsyncSession,
        refresh_tokens: RefreshTokenService,
        blacklist: AccessTokenBlacklist,
        verify_email_tokens: SingleUseTokenStore,
        reset_password_tokens: SingleUseTokenStore,
    ):
        self.db = db
        self.refresh_tokens = refresh_tokens
        self.blacklist = blacklist
        self.verify_email_tokens = verify_email_tokens
        self.reset_password_tokens = reset_password_tokens

    async def _get_by_email(self, email: str) -> User | None:
        result = await self.db.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

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
        await self.db.commit()
        await self.db.refresh(user)

        token = await self.verify_email_tokens.issue(user.id)
        await send_email(
            to=user.email,
            subject="Verify your Kontributa account",
            body=f"Your verification token is: {token}",
        )
        return user

    async def verify_email(self, token: str) -> bool:
        user_id = await self.verify_email_tokens.consume(token)
        if user_id is None:
            raise AuthError("invalid or expired verification token", code="token_invalid")

        user = await self.db.get(User, user_id)
        if user is None:
            raise NotFoundError("user not found")

        user.is_verified = True
        await self.db.commit()
        return True

    async def login(self, payload: LoginRequest) -> tuple[str, str, str]:
        user = await self._get_by_email(payload.email)
        if user is None or not verify_password(payload.password, user.password_hash):
            raise AuthError("invalid email or password", code="invalid_credentials")

        access = create_access_token(user.id, user.role.value)
        refresh_token, _ = await self.refresh_tokens.issue(user.id)
        return access.token, refresh_token, user.role.value

    async def refresh(self, payload: RefreshTokenRequest) -> tuple[str, str]:
        new_refresh_token, user_id = await self.refresh_tokens.rotate(payload.refresh_token)

        user = await self.db.get(User, user_id)
        if user is None:
            raise NotFoundError("user not found")

        access = create_access_token(user.id, user.role.value)
        return access.token, new_refresh_token

    async def logout(self, refresh_token: str, access_jti: str, access_exp) -> None:
        await self.refresh_tokens.revoke_by_token(refresh_token)
        await self.blacklist.blacklist(access_jti, access_exp)

    async def forgot_password(self, email: str) -> None:
        user = await self._get_by_email(email)
        if user is None:
            return
        token = await self.reset_password_tokens.issue(user.id)
        await send_email(
            to=user.email,
            subject="Reset your Kontributa password",
            body=f"Your password reset token is: {token}",
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
