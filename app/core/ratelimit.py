from fastapi import Depends, Request
from redis.asyncio import Redis

from app.core.exceptions import AppException
from app.core.redis import get_redis


class RateLimitedError(AppException):
    status_code = 429
    code = "rate_limited"


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_limit_by_ip(scope: str, max_requests: int, window_seconds: int):
    """FastAPI dependency factory: a fixed-window counter keyed by client IP
    + scope, stored in Redis. Intended for unauthenticated, email-triggering
    endpoints (register, forgot-password) where there's no user id yet to
    key on -- the goal is bounding how many SendByte sends a single source
    can trigger, not perfect per-account fairness."""

    async def dependency(request: Request, redis: Redis = Depends(get_redis)) -> None:
        key = f"ratelimit:{scope}:{_client_ip(request)}"
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, window_seconds)
        if count > max_requests:
            raise RateLimitedError(
                f"too many requests -- try again in a few minutes", code="rate_limited"
            )

    return dependency


async def check_rate_limit(redis: Redis, scope: str, identity: str, max_requests: int, window_seconds: int) -> None:
    """Same fixed-window counter as rate_limit_by_ip, but callable directly
    from inside a route handler once an authenticated identity (e.g. an
    admin id) is already available, rather than as a standalone Depends."""
    key = f"ratelimit:{scope}:{identity}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, window_seconds)
    if count > max_requests:
        raise RateLimitedError("too many requests -- try again later", code="rate_limited")
