from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class ContributionDetailResponse(BaseModel):
    id: UUID
    purse_id: UUID
    # Exactly one of these two is ever set -- see
    # Contribution.ck_contribution_exactly_one_owner.
    member_id: Optional[UUID] = None
    group_admin_id: Optional[UUID] = None
    owner_type: Literal["member", "admin"]
    status: str
    # Display-layer only -- see compute_display_status's docstring. status
    # above stays the real, unmassaged value.
    display_status: str
    # Money is always a string on the wire (never a bare JSON number, to
    # avoid float precision loss) -- see known-limitations.md.
    amount_expected: str
    amount_received: str
    account_number: Optional[str] = None
    bank_name: Optional[str] = None
    checkout_url: Optional[str] = None
    invoice_expires_at: Optional[datetime] = None
    paid_at: Optional[datetime] = None


class GenerateInvoiceResponse(BaseModel):
    account_number: str
    bank_name: str
    amount: str
    expires_at: datetime
    checkout_url: Optional[str] = None


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
