import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Optional
from uuid import UUID

import httpx

from app.core.config import settings

logger = logging.getLogger("kontributa.realtime")

ABLY_REST_HOST = "https://main.realtime.ably.net"

# Default validity for a token handed to the frontend -- long enough to
# cover a member sitting on the generate-invoice screen through the
# invoice's own expiry window, short enough that a leaked token is
# useless soon after.
DEFAULT_TOKEN_TTL_MS = 60 * 60 * 1000


def contribution_channel_name(contribution_id: UUID) -> str:
    return f"contribution:{contribution_id}"


class RealtimeService:
    """Thin, dependency-free Ably client (same philosophy as
    MonnifyClient -- raw httpx over the vendor SDK) covering exactly the
    two things this app needs: signing a capability-scoped TokenRequest
    for the frontend to exchange itself (Ably's documented token-auth
    pattern -- the raw API key/secret never leaves this service), and
    publishing a message server-side when a Contribution's status
    changes."""

    def __init__(self, api_key: Optional[str] = None):
        self._api_key = api_key if api_key is not None else settings.ABLY_API_KEY
        self._key_name, _, self._key_secret = self._api_key.partition(":")

    @property
    def configured(self) -> bool:
        return bool(self._api_key and self._key_secret)

    def create_token_request(
        self, capability: dict[str, list[str]], ttl_ms: int = DEFAULT_TOKEN_TTL_MS, client_id: str = ""
    ) -> dict:
        """Builds and signs an Ably TokenRequest (per Ably's Token Request
        Spec: HMAC-SHA256 of keyName\\nttl\\ncapability\\nclientId\\ntimestamp\\nnonce\\n,
        keyed by the key secret, base64-encoded) -- the frontend exchanges
        this with Ably directly via authCallback; this service's job ends
        at handing back a signed request scoped to exactly the caller's
        own channel(s), never a token or the raw key itself."""
        timestamp_ms = int(time.time() * 1000)
        nonce = secrets.token_hex(16)
        capability_json = json.dumps(capability, separators=(",", ":"), sort_keys=True)
        canonical = (
            "\n".join(
                [self._key_name, str(ttl_ms), capability_json, client_id, str(timestamp_ms), nonce]
            )
            + "\n"
        )
        mac = base64.b64encode(
            hmac.new(self._key_secret.encode(), canonical.encode(), hashlib.sha256).digest()
        ).decode()
        request = {
            "keyName": self._key_name,
            "ttl": ttl_ms,
            "capability": capability_json,
            "timestamp": timestamp_ms,
            "nonce": nonce,
            "mac": mac,
        }
        # Ably rejects clientId as an empty string outright (must be a
        # real value or omitted) -- confirmed against a real token
        # exchange. The signature above still includes it as an empty
        # signed component either way, per the Token Request Spec.
        if client_id:
            request["clientId"] = client_id
        return request

    async def publish(self, channel: str, name: str, data: dict) -> None:
        """Best-effort only -- this is a live nudge layered on top of the
        real source of truth (Contribution.status, unaffected either way),
        never allowed to break the webhook/reconciliation flow that
        called it. Silently no-ops if Ably isn't configured at all."""
        if not self.configured:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                resp = await http.post(
                    f"{ABLY_REST_HOST}/channels/{channel}/messages",
                    json={"name": name, "data": data},
                    auth=(self._key_name, self._key_secret),
                )
            resp.raise_for_status()
        except Exception:
            logger.warning("realtime publish failed for channel %s", channel, exc_info=True)


realtime_service = RealtimeService()


def get_realtime_service() -> RealtimeService:
    return realtime_service
