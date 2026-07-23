from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class ReconciliationRunRequest(BaseModel):
    purse_id: Optional[UUID] = None


class ReconciliationRunResponse(BaseModel):
    checked: int
    updated: int


class WebhookEventListItem(BaseModel):
    id: UUID
    provider_event_id: str
    signature_valid: bool
    processed: bool
    received_at: datetime


class FlaggedContributionItem(BaseModel):
    id: UUID
    purse_id: UUID
    member_id: UUID
    # Money is always a string on the wire (never a bare JSON number, to
    # avoid float precision loss) -- see known-limitations.md.
    amount_expected: str
    amount_received: str
