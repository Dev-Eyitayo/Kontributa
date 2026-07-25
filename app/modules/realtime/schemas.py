from typing import Optional

from pydantic import BaseModel


class RealtimeTokenResponse(BaseModel):
    """A signed Ably TokenRequest, not a token itself -- the frontend's
    Ably client exchanges this with Ably directly. Field names/casing
    match Ably's TokenRequest spec exactly (not snake_case) since the
    frontend hands this object to the Ably SDK verbatim."""

    keyName: str
    ttl: int
    capability: str
    clientId: Optional[str] = None
    timestamp: int
    nonce: str
    mac: str
