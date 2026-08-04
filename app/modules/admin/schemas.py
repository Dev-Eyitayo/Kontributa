from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class ReconciliationRunRequest(BaseModel):
    purse_id: Optional[UUID] = None
    force: Optional[bool] = True


class ReconciliationRunResponse(BaseModel):
    checked: int
    updated: int


class WebhookEventListItem(BaseModel):
    id: UUID
    provider_event_id: str
    signature_valid: bool
    processed: bool
    received_at: datetime


class WebhookEventDetailOut(BaseModel):
    id: UUID
    provider_event_id: str
    raw_payload: str
    signature_valid: bool
    processed: bool
    processing_error: Optional[str] = None
    received_at: datetime


class FlaggedContributionItem(BaseModel):
    id: UUID
    purse_id: UUID
    # Exactly one of these two is ever set -- an admin's own contribution
    # to their group's purse can be flagged for review too, same as any
    # member's.
    member_id: Optional[UUID] = None
    group_admin_id: Optional[UUID] = None
    # Money is always a string on the wire (never a bare JSON number, to
    # avoid float precision loss) -- see known-limitations.md.
    amount_expected: str
    amount_received: str
