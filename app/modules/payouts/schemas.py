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


class AvailableBalanceResponse(BaseModel):
    purse_id: UUID
    collected_total: Decimal
    paid_out_total: Decimal
    available_balance: Decimal
