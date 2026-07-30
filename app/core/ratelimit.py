from typing import Optional
from fastapi import Depends, Request
from redis.asyncio import Redis

from app.core.exceptions import AppException
from app.core.redis import get_redis


class RateLimitedError(AppException):
    status_code = 429
    code = "rate_limited"

    def __init__(self, message: str, retry_after: Optional[int] = None, code: Optional[str] = None):
        details = {"retry_after": retry_after} if retry_after is not None else None
        super().__init__(message, details=details, code=code)
        self.retry_after = retry_after


def _client_ip(request: Request) -> str:
    """Extracts true client IP, taking trusted reverse proxy headers
    (CF-Connecting-IP, X-Forwarded-For) into account before falling back
    to request.client.host."""
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip.strip()

    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        # X-Forwarded-For: client, proxy1, proxy2 -- take first IP (client)
        parts = [p.strip() for p in forwarded_for.split(",") if p.strip()]
        if parts:
            return parts[0]

    return request.client.host if request.client else "unknown"


async def _increment_and_check_counter(
    redis: Redis, key: str, max_requests: int, window_seconds: int
) -> tuple[int, int]:
    """Atomically increments key in Redis, sets expiration on first write,
    and returns (count, ttl_remaining_seconds)."""
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.ttl(key)
        results = await pipe.execute()
        count = results[0]
        ttl = results[1]

        # If key was just created (count == 1) or lost TTL, set expiration
        if count == 1 or ttl < 0:
            await redis.expire(key, window_seconds)
            ttl = window_seconds

        return count, max(1, ttl if ttl > 0 else window_seconds)


def rate_limit_by_ip(scope: str, max_requests: int, window_seconds: int):
    """FastAPI dependency factory: an atomic fixed-window counter keyed by
    client IP + scope stored in Redis."""

    async def dependency(request: Request, redis: Redis = Depends(get_redis)) -> None:
        client_ip = _client_ip(request)
        key = f"ratelimit:{scope}:{client_ip}"
        count, ttl = await _increment_and_check_counter(redis, key, max_requests, window_seconds)
        if count > max_requests:
            raise RateLimitedError(
                f"Too many requests. Please try again in {ttl} seconds.",
                retry_after=ttl,
                code="rate_limited",
            )

    return dependency


async def check_rate_limit(
    redis: Redis, scope: str, identity: str, max_requests: int, window_seconds: int
) -> None:
    """Callable directly from route handlers or services to enforce rate limits
    on an authenticated identity or target email address."""
    key = f"ratelimit:{scope}:{identity.lower()}"
    count, ttl = await _increment_and_check_counter(redis, key, max_requests, window_seconds)
    if count > max_requests:
        raise RateLimitedError(
            f"Too many requests. Please try again in {ttl} seconds.",
            retry_after=ttl,
            code="rate_limited",
        )


async def check_dual_rate_limit(
    request: Request,
    redis: Redis,
    scope: str,
    identity: str,
    max_requests_ip: int,
    max_requests_identity: int,
    window_seconds: int,
) -> None:
    """Enforces dual-key rate limiting: both on client IP AND target identity."""
    client_ip = _client_ip(request)
    await check_rate_limit(redis, f"{scope}:ip", client_ip, max_requests_ip, window_seconds)
    await check_rate_limit(redis, f"{scope}:id", identity, max_requests_identity, window_seconds)

