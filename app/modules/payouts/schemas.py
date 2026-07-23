from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class CreatePayoutRequest(BaseModel):
    group_id: UUID
    purse_id: Optional[UUID] = None
    amount: Decimal = Field(gt=0)


class RejectPayoutRequest(BaseModel):
    reason: str


class PayoutCreateResponse(BaseModel):
    id: UUID
    status: str
    amount: str


class PayoutListItem(BaseModel):
    id: UUID
    group_id: UUID
    purse_id: Optional[UUID] = None
    amount: str
    status: str
    requested_by: UUID
    created_at: datetime


class PayoutDetailResponse(BaseModel):
    id: UUID
    status: str
    amount: str
    monnify_transfer_ref: Optional[str] = None
    failure_reason: Optional[str] = None


class PayoutApproveResponse(BaseModel):
    id: UUID
    status: str
    approved_by: UUID


class PayoutRejectResponse(BaseModel):
    id: UUID
    status: str
    reason: Optional[str] = None
