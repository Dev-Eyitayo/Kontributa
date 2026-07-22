from datetime import datetime
from decimal import Decimal
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
    amount_expected: Decimal
    amount_received: Decimal
