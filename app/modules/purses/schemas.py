from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

EnrollModeLiteral = Literal["snapshot", "auto_enroll"]


class CreatePurseRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    amount: Decimal = Field(gt=0)
    deadline: datetime
    cohort: Optional[str] = None
    enroll_mode: EnrollModeLiteral


class UpdatePurseRequest(BaseModel):
    amount: Optional[Decimal] = Field(default=None, gt=0)
    deadline: Optional[datetime] = None


class ContributionListItem(BaseModel):
    member_id: UUID
    name: str
    member_id_number: Optional[str] = None
    status: str
    amount_received: Decimal
    paid_at: Optional[datetime] = None


class PurseSummary(BaseModel):
    paid_count: int
    pending_count: int
    expired_count: int
    flagged_count: int
    total_collected: Decimal
    percent_complete: float
