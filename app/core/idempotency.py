import hashlib
import json
from datetime import timedelta
from typing import Any, Optional
from uuid import UUID

from fastapi import Depends, Header
from redis.asyncio import Redis

from app.core.exceptions import AppException, ConflictError
from app.core.redis import get_redis

DEFAULT_IDEMPOTENCY_TTL = timedelta(hours=24)


class IdempotencyConflictError(AppException):
    status_code = 409
    code = "idempotency_key_conflict"


def fingerprint(payload: Any) -> str:
    """Stable hash of a request body, used to detect the same Idempotency-Key
    being replayed against a materially different request."""
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class IdempotencyStore:
    """
    Redis-backed Idempotency-Key support (Stripe-style).

    Usage per endpoint:
        cached = await store.begin(scope, actor_id, idempotency_key, fingerprint(payload))
        if cached is not None:
            return cached["status_code"], cached["body"]
        try:
            status_code, body = await do_the_work()
        except Exception:
            await store.release(scope, actor_id, idempotency_key)
            raise
        await store.complete(scope, actor_id, idempotency_key, fingerprint(payload), status_code, body)

    `begin` either returns a previously-completed response (safe to replay),
    raises IdempotencyConflictError if the same key was used with a different
    body, raises ConflictError if a request with this key is still in flight,
    or reserves the key and returns None so the caller proceeds.
    """

    def __init__(self, redis: Redis, ttl: timedelta = DEFAULT_IDEMPOTENCY_TTL):
        self._redis = redis
        self._ttl = ttl

    def _key(self, scope: str, actor_id: UUID, idempotency_key: str) -> str:
        return f"idempotency:{scope}:{actor_id}:{idempotency_key}"

    async def begin(
        self, scope: str, actor_id: UUID, idempotency_key: str, request_fingerprint: str
    ) -> Optional[dict]:
        key = self._key(scope, actor_id, idempotency_key)
        reserved = await self._redis.set(
            key,
            json.dumps({"fingerprint": request_fingerprint, "response": None}),
            nx=True,
            ex=int(self._ttl.total_seconds()),
        )
        if reserved:
            return None

        raw = await self._redis.get(key)
        if raw is None:
            # Reservation expired between the failed SET NX and this GET; proceed fresh.
            return None

        data = json.loads(raw)
        if data["fingerprint"] != request_fingerprint:
            raise IdempotencyConflictError(
                "this Idempotency-Key was already used with a different request body"
            )
        if data["response"] is None:
            raise ConflictError(
                "a request with this idempotency key is already being processed",
                code="idempotency_in_progress",
            )
        return data["response"]

    async def complete(
        self,
        scope: str,
        actor_id: UUID,
        idempotency_key: str,
        request_fingerprint: str,
        status_code: int,
        body: Any,
    ) -> None:
        key = self._key(scope, actor_id, idempotency_key)
        await self._redis.set(
            key,
            json.dumps({"fingerprint": request_fingerprint, "response": {"status_code": status_code, "body": body}}),
            ex=int(self._ttl.total_seconds()),
        )

    async def release(self, scope: str, actor_id: UUID, idempotency_key: str) -> None:
        await self._redis.delete(self._key(scope, actor_id, idempotency_key))


def get_idempotency_store(redis: Redis = Depends(get_redis)) -> IdempotencyStore:
    return IdempotencyStore(redis)


def get_idempotency_key(
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> Optional[str]:
    """Optional by default: an endpoint that wants to require it should check for None itself."""
    return idempotency_key
