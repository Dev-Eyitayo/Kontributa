import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional
from uuid import UUID, uuid4

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.exceptions import AuthError, ForbiddenError
from app.core.redis import get_redis

bearer_scheme = HTTPBearer(auto_error=False)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Access tokens (stateless JWT, short-lived)
# ---------------------------------------------------------------------------


@dataclass
class AccessToken:
    token: str
    jti: str
    expires_at: datetime


def create_access_token(user_id: UUID, role: str) -> AccessToken:
    jti = uuid4().hex
    now = _utcnow()
    expires_at = now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": str(user_id),
        "role": role,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return AccessToken(token=token, jti=jti, expires_at=expires_at)


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise AuthError("access token expired", code="token_expired")
    except jwt.InvalidTokenError:
        raise AuthError("invalid access token", code="token_invalid")


# ---------------------------------------------------------------------------
# Access-token blacklist (logout support)
# ---------------------------------------------------------------------------


class AccessTokenBlacklist:
    def __init__(self, redis: Redis):
        self._redis = redis

    @staticmethod
    def _key(jti: str) -> str:
        return f"blacklist:{jti}"

    async def blacklist(self, jti: str, expires_at: datetime) -> None:
        ttl = max(int((expires_at - _utcnow()).total_seconds()), 1)
        await self._redis.set(self._key(jti), "1", ex=ttl)

    async def is_blacklisted(self, jti: str) -> bool:
        return bool(await self._redis.exists(self._key(jti)))


def get_access_token_blacklist(redis: Redis = Depends(get_redis)) -> AccessTokenBlacklist:
    return AccessTokenBlacklist(redis)


# ---------------------------------------------------------------------------
# Refresh tokens (Redis-backed, rotating, with reuse detection)
# ---------------------------------------------------------------------------


class RefreshTokenService:
    """
    Each refresh token belongs to a "family" created at login. Rotating a
    refresh token issues a new token and repoints the family's "current"
    pointer at it, but the old token's record is left in place (until its
    own TTL) rather than deleted.

    If a token is presented that still resolves (not expired) but is no
    longer the family's current token, that is a reuse of an
    already-rotated token -- a signal of token theft -- so the entire
    family is revoked, forcing re-login.
    """

    def __init__(self, redis: Redis):
        self._redis = redis
        self._ttl = timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)

    @staticmethod
    def _token_key(token: str) -> str:
        return f"refresh:{token}"

    @staticmethod
    def _family_key(family_id: str) -> str:
        return f"refresh_family:{family_id}"

    async def issue(self, user_id: UUID, family_id: Optional[str] = None) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        family_id = family_id or uuid4().hex
        payload = json.dumps({"user_id": str(user_id), "family_id": family_id})
        ttl = int(self._ttl.total_seconds())
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.set(self._token_key(token), payload, ex=ttl)
            pipe.set(self._family_key(family_id), token, ex=ttl)
            await pipe.execute()
        return token, family_id

    async def rotate(self, old_token: str) -> tuple[str, UUID]:
        raw = await self._redis.get(self._token_key(old_token))
        if raw is None:
            raise AuthError("invalid or expired refresh token", code="refresh_invalid")

        parsed = json.loads(raw)
        user_id = UUID(parsed["user_id"])
        family_id = parsed["family_id"]

        current = await self._redis.get(self._family_key(family_id))
        if current != old_token:
            await self.revoke_family(family_id)
            raise AuthError(
                "refresh token reuse detected; session revoked",
                code="refresh_reuse_detected",
            )

        new_token, _ = await self.issue(user_id, family_id=family_id)
        return new_token, user_id

    async def revoke_by_token(self, token: str) -> None:
        raw = await self._redis.get(self._token_key(token))
        if raw is None:
            return
        parsed = json.loads(raw)
        await self.revoke_family(parsed["family_id"])

    async def revoke_family(self, family_id: str) -> None:
        current = await self._redis.get(self._family_key(family_id))
        if current is not None:
            await self._redis.delete(self._token_key(current))
        await self._redis.delete(self._family_key(family_id))


def get_refresh_token_service(redis: Redis = Depends(get_redis)) -> RefreshTokenService:
    return RefreshTokenService(redis)


# ---------------------------------------------------------------------------
# Single-use, short-TTL Redis tokens (email verification, password reset)
# ---------------------------------------------------------------------------


class SingleUseTokenStore:
    def __init__(self, redis: Redis, prefix: str, ttl: timedelta):
        self._redis = redis
        self._prefix = prefix
        self._ttl = ttl

    def _key(self, token: str) -> str:
        return f"{self._prefix}:{token}"

    async def issue(self, user_id: UUID) -> str:
        token = secrets.token_urlsafe(32)
        await self._redis.set(self._key(token), str(user_id), ex=int(self._ttl.total_seconds()))
        return token

    async def consume(self, token: str) -> Optional[UUID]:
        key = self._key(token)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.get(key)
            pipe.delete(key)
            raw, _ = await pipe.execute()
        if raw is None:
            return None
        return UUID(raw)


def get_email_verification_token_store(redis: Redis = Depends(get_redis)) -> SingleUseTokenStore:
    return SingleUseTokenStore(
        redis, "verify_email", timedelta(hours=settings.EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS)
    )


def get_password_reset_token_store(redis: Redis = Depends(get_redis)) -> SingleUseTokenStore:
    return SingleUseTokenStore(
        redis, "reset_password", timedelta(minutes=settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES)
    )


# ---------------------------------------------------------------------------
# Request-scoped identity dependencies
# ---------------------------------------------------------------------------


class UserRole(str, Enum):
    GROUP_ADMIN = "group_admin"
    MEMBER = "member"


@dataclass
class CurrentUser:
    id: UUID
    role: str
    jti: str
    expires_at: datetime


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    blacklist: AccessTokenBlacklist = Depends(get_access_token_blacklist),
) -> CurrentUser:
    if credentials is None:
        raise AuthError("missing bearer token", code="missing_token")

    payload = decode_access_token(credentials.credentials)
    if await blacklist.is_blacklisted(payload["jti"]):
        raise AuthError("token has been revoked", code="token_revoked")

    return CurrentUser(
        id=UUID(payload["sub"]),
        role=payload["role"],
        jti=payload["jti"],
        expires_at=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
    )


def require_role(role: UserRole):
    async def dependency(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role != role.value:
            raise ForbiddenError(f"requires role '{role.value}'")
        return user

    return dependency


get_current_group_admin_user = require_role(UserRole.GROUP_ADMIN)
get_current_member_user = require_role(UserRole.MEMBER)


async def get_current_admin_user(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    from app.modules.auth.models import User

    row = await db.get(User, user.id)
    if row is None or not row.is_platform_admin:
        raise ForbiddenError("admin access required")
    return user


async def require_verified_email(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    """Per api-spec.md: 'Unverified email may still log in but is blocked
    from creating purses/paying until verified.' Composes with a role
    dependency (e.g. Depends(get_current_group_admin_user)) rather than
    replacing it -- this only adds the verification gate on top."""
    from app.modules.auth.models import User

    row = await db.get(User, user.id)
    if row is None or not row.is_verified:
        raise ForbiddenError("email verification required for this action", code="email_not_verified")
    return user
