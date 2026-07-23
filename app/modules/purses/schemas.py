from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

EnrollModeLiteral = Literal["snapshot", "auto_enroll"]


def _assume_utc_if_naive(value: Optional[datetime]) -> Optional[datetime]:
    # A bare date ("2026-07-24") or a naive datetime string parses to a
    # tzinfo-less datetime -- comparing that against datetime.now(timezone.utc)
    # elsewhere raises TypeError instead of the intended validation error.
    # Treat anything with no offset as UTC rather than rejecting it.
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class CreatePurseRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    amount: Decimal = Field(gt=0)
    deadline: datetime
    cohort: Optional[str] = None
    enroll_mode: EnrollModeLiteral

    _normalize_deadline = field_validator("deadline")(_assume_utc_if_naive)


class UpdatePurseRequest(BaseModel):
    amount: Optional[Decimal] = Field(default=None, gt=0)
    deadline: Optional[datetime] = None

    _normalize_deadline = field_validator("deadline")(_assume_utc_if_naive)


class AddMemberToPurseRequest(BaseModel):
    member_id: UUID


class AddMemberToPurseResponse(BaseModel):
    id: UUID
    purse_id: UUID
    member_id: UUID
    status: str
    amount_expected: str


class ContributionListItem(BaseModel):
    member_id: UUID
    name: str
    member_id_number: Optional[str] = None
    status: str
    # Money is always a string on the wire (never a bare JSON number, to
    # avoid float precision loss) -- see known-limitations.md.
    amount_received: str
    paid_at: Optional[datetime] = None


class PurseSummary(BaseModel):
    paid_count: int
    pending_count: int
    expired_count: int
    flagged_count: int
    # Money is always a string on the wire (never a bare JSON number, to
    # avoid float precision loss) -- see known-limitations.md.
    total_collected: str
    percent_complete: float


class PurseOut(BaseModel):
    id: UUID
    title: str
    amount: str
    deadline: str
    status: str


class PurseListItemAdminOut(PurseOut):
    paid_count: int
    total_count: int


class PurseListItemMemberOut(PurseOut):
    contribution_status: str


class PurseDetailAdminOut(PurseOut):
    enroll_mode: EnrollModeLiteral
    paid_count: int
    total_count: int


class PurseDetailMemberOut(PurseOut):
    enroll_mode: EnrollModeLiteral
    contribution_status: str


class PurseUpdateResponse(BaseModel):
    id: UUID
    amount: str
    deadline: str


class PurseStatusResponse(BaseModel):
    id: UUID
    status: str


class AvailableBalanceOut(BaseModel):
    purse_id: UUID
    collected_total: str
    paid_out_total: str
    available_balance: str
