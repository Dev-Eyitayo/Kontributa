from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class ContributionDetailResponse(BaseModel):
    id: UUID
    purse_id: UUID
    member_id: UUID
    status: str
    amount_expected: Decimal
    amount_received: Decimal
    account_number: Optional[str] = None
    invoice_expires_at: Optional[datetime] = None


class GenerateInvoiceResponse(BaseModel):
    account_number: str
    bank_name: str
    amount: Decimal
    expires_at: datetime


class MarkManualRequest(BaseModel):
    amount_received: Decimal = Field(gt=0)
    note: str


class MarkManualResponse(BaseModel):
    id: UUID
    status: str


class ContributionHistoryItem(BaseModel):
    from_status: str
    to_status: str
    actor_type: str
    actor_id: Optional[UUID] = None
    note: Optional[str] = None
    created_at: datetime


class ResolveFlagRequest(BaseModel):
    resolution: Literal["accept_partial", "request_topup", "refund"]


class ResolveFlagResponse(BaseModel):
    id: UUID
    status: str
