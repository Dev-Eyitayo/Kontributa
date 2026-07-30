import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional
from uuid import UUID, uuid4

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_cookies import ACCESS_TOKEN_COOKIE
from app.core.config import settings
from app.core.db import get_db
from app.core.exceptions import AuthError, ForbiddenError
from app.core.redis import get_redis

bearer_scheme = HTTPBearer(auto_error=False)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class AccessToken:
    token: str
    jti: str
    expires_at: datetime


def create_access_token(user_id: UUID, role: str, family_id: Optional[str] = None) -> AccessToken:
    jti = uuid4().hex
    now = _utcnow()
    expires_at = now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": str(user_id),
        "role": role,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        # The refresh token family this access token was minted alongside
        # -- lets an authenticated request (e.g. POST /auth/heartbeat)
        # identify which session's activity to record, without carrying
        # the refresh token itself around. None for tokens minted without
        # one (a handful of test helpers call create_access_token directly,
        # bypassing login) -- those simply can't participate in the
        # inactivity mechanism, nothing else depends on this being set.
        "fam": family_id,
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

    async def peek(self, token: str) -> Optional[tuple[UUID, str]]:
        """Read-only (user_id, family_id) lookup for a still-valid token,
        without rotating it -- used to check the inactivity gate (see
        SessionActivityService below) before committing to a rotation."""
        raw = await self._redis.get(self._token_key(token))
        if raw is None:
            return None
        parsed = json.loads(raw)
        return UUID(parsed["user_id"]), parsed["family_id"]

    async def rotate(self, old_token: str) -> tuple[str, UUID, str]:
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
        return new_token, user_id, family_id

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


class SessionActivityService:
    """Tracks genuine human activity per refresh-token family -- a
    completely separate mechanism from that token's own absolute TTL (see
    RefreshTokenService). Only two things are ever allowed to touch this:
    login (a real human just typed credentials) and POST /auth/heartbeat
    (a debounced signal the frontend fires only on real mouse/keyboard/
    touch/scroll interaction, at most once a minute -- see
    use-idle-session.ts). Nothing else, especially not the silent
    access-token refresh itself, which fires on its own ~15-minute
    schedule regardless of whether anyone is actually present -- if that
    counted as activity, an abandoned-but-open tab with any ambient
    background traffic would never actually time out.
    """

    def __init__(self, redis: Redis):
        self._redis = redis

    @staticmethod
    def _key(family_id: str) -> str:
        return f"last_active:{family_id}"

    async def touch(self, family_id: str) -> None:
        # Same TTL as the refresh token family itself -- once the family
        # is gone, its activity record is meaningless and may as well
        # expire alongside it rather than lingering as an orphan key.
        ttl = int(timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS).total_seconds())
        await self._redis.set(self._key(family_id), str(int(_utcnow().timestamp())), ex=ttl)

    async def is_stale(self, family_id: str, threshold_minutes: int) -> bool:
        """True only when there IS a recorded last-active time and it's
        older than the threshold. No recorded activity at all (e.g. the
        very first refresh right after login, before login's own seed
        write -- see AuthService.login -- or before any heartbeat has had
        a chance to fire) is never treated as stale on that basis alone."""
        raw = await self._redis.get(self._key(family_id))
        if raw is None:
            return False
        last_active = datetime.fromtimestamp(int(raw), tz=timezone.utc)
        return _utcnow() - last_active > timedelta(minutes=threshold_minutes)

    async def clear(self, family_id: str) -> None:
        await self._redis.delete(self._key(family_id))


def get_session_activity_service(redis: Redis = Depends(get_redis)) -> SessionActivityService:
    return SessionActivityService(redis)


def _generate_numeric_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


class SingleUseTokenStore:
    def __init__(self, redis: Redis, prefix: str, ttl: timedelta):
        self._redis = redis
        self._prefix = prefix
        self._ttl = ttl

    def _key(self, token: str) -> str:
        return f"{self._prefix}:{token}"

    async def issue(self, user_id: UUID) -> str:
        for _ in range(5):
            token = _generate_numeric_code()
            reserved = await self._redis.set(
                self._key(token), str(user_id), ex=int(self._ttl.total_seconds()), nx=True
            )
            if reserved:
                return token
        raise RuntimeError("failed to issue a unique single-use code after 5 attempts")

    async def consume(self, token: str) -> Optional[UUID]:
        key = self._key(token)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.get(key)
            pipe.delete(key)
            raw, _ = await pipe.execute()
        if raw is None:
            return None
        return UUID(raw)

    async def peek(self, token: str) -> Optional[UUID]:
        """Reads without consuming -- lets a caller validate extra context
        (e.g. the code belongs to the claimed email) before deciding
        whether this attempt should actually burn the code."""
        raw = await self._redis.get(self._key(token))
        if raw is None:
            return None
        return UUID(raw)

    async def delete(self, token: str) -> None:
        await self._redis.delete(self._key(token))


def get_email_verification_token_store(redis: Redis = Depends(get_redis)) -> SingleUseTokenStore:
    return SingleUseTokenStore(
        redis, "verify_email", timedelta(minutes=settings.EMAIL_VERIFICATION_TOKEN_EXPIRE_MINUTES)
    )


def get_password_reset_token_store(redis: Redis = Depends(get_redis)) -> SingleUseTokenStore:
    return SingleUseTokenStore(
        redis, "reset_password", timedelta(minutes=settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES)
    )


def get_password_reset_grant_token_store(redis: Redis = Depends(get_redis)) -> SingleUseTokenStore:
    return SingleUseTokenStore(
        redis, "reset_grant", timedelta(minutes=15)
    )



class UserRole(str, Enum):
    GROUP_ADMIN = "group_admin"
    MEMBER = "member"


@dataclass
class CurrentUser:
    id: UUID
    role: str
    jti: str
    expires_at: datetime
    # The refresh token family this access token was minted alongside --
    # None for a token minted without one (see create_access_token). Only
    # POST /auth/heartbeat actually reads this.
    family_id: Optional[str] = None


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    blacklist: AccessTokenBlacklist = Depends(get_access_token_blacklist),
) -> CurrentUser:
    # Bearer header takes priority when present -- this keeps Bearer mode
    # working completely unchanged. Only falls back to the httpOnly cookie
    # when there's no header at all AND cookie mode is actually on, so
    # this is a pure addition with zero behavior change while the flag is
    # off (no login flow in that mode ever sets the cookie in the first
    # place, so it would never be present anyway).
    token = credentials.credentials if credentials is not None else None
    if token is None and settings.USE_HTTPONLY_COOKIES:
        token = request.cookies.get(ACCESS_TOKEN_COOKIE)

    if token is None:
        raise AuthError("missing bearer token", code="missing_token")

    payload = decode_access_token(token)
    if await blacklist.is_blacklisted(payload["jti"]):
        raise AuthError("token has been revoked", code="token_revoked")

    return CurrentUser(
        id=UUID(payload["sub"]),
        role=payload["role"],
        jti=payload["jti"],
        expires_at=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
        family_id=payload.get("fam"),
    )


async def get_current_group_admin_user(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    """An account can now hold an active GroupAdmin row for some groups and
    an active Member row for others at the same time (dual identity), so
    "is this a group admin" is a live DB fact, not User.role -- a static
    value stamped once at registration that never reflects an identity
    acquired later (e.g. a Member who creates their own group via
    POST /group-admins/onboard). Every group-scoped endpoint downstream of
    this still narrows further to a *specific* group_id (see
    GroupAdminService.get_admin_for_group); this only proves "at least one,
    somewhere".

    A platform admin has no GroupAdmin row at all (they're not any group's
    own admin) but must still pass this -- several group-scoped endpoints
    (e.g. purses/router.py::list_contributions) rely on reaching their own
    body to do the real cross-group is_platform_admin check themselves.
    This used to fall out for free from the old role-based check (a
    platform admin's JWT role claim is also "group_admin"); the DB-backed
    check above needs the same bypass spelled out explicitly."""
    from app.modules.auth.models import User
    from app.modules.group_admins.models import GroupAdmin

    user_row = await db.get(User, user.id)
    if user_row is not None and user_row.is_platform_admin:
        return user

    result = await db.execute(
        select(GroupAdmin.id).where(GroupAdmin.user_id == user.id, GroupAdmin.is_active_admin.is_(True)).limit(1)
    )
    if result.scalar_one_or_none() is None:
        raise ForbiddenError("requires an active group admin identity")
    return user


async def get_current_member_user(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    """Mirrors get_current_group_admin_user above for the Member side --
    an existing Group Admin who has also joined a group as a Member (see
    MemberService.join_additional_group) must be able to reach Member-only
    endpoints too, which a static User.role check would forever block."""
    from app.modules.members.models import Member

    result = await db.execute(
        select(Member.id).where(Member.user_id == user.id, Member.removed_at.is_(None)).limit(1)
    )
    if result.scalar_one_or_none() is None:
        raise ForbiddenError("requires an active member identity")
    return user


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
    from app.modules.auth.models import User

    row = await db.get(User, user.id)
    if row is None or not row.is_verified:
        raise ForbiddenError("email verification required for this action", code="email_not_verified")
    return user
