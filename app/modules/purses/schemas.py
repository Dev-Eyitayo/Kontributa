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
    # Explicit rather than derived from "the admin's one group" -- an admin
    # can now actively manage more than one group (see known-limitations.md
    # / the multi-group admin support), so every group-scoped write names
    # its target group directly.
    group_id: UUID
    title: str = Field(min_length=1, max_length=255)
    amount: Decimal = Field(gt=0)
    deadline: datetime
    enroll_mode: EnrollModeLiteral

    _normalize_deadline = field_validator("deadline")(_assume_utc_if_naive)


class UpdatePurseRequest(BaseModel):
    amount: Optional[Decimal] = Field(default=None, gt=0)
    deadline: Optional[datetime] = None

    _normalize_deadline = field_validator("deadline")(_assume_utc_if_naive)


class ReopenPurseRequest(BaseModel):
    new_deadline: datetime

    _normalize_deadline = field_validator("new_deadline")(_assume_utc_if_naive)


class AddMemberToPurseRequest(BaseModel):
    member_id: UUID


class AddMemberToPurseResponse(BaseModel):
    id: UUID
    purse_id: UUID
    member_id: UUID
    status: str
    amount_expected: str


class ContributionListItem(BaseModel):
    id: UUID
    # Exactly one of member_id/owner_type=="member" is ever set -- an
    # admin contributing to their own group's purse is just another row
    # in this same list (see Contribution.group_admin_id), never a
    # separate section. member_id_number is likewise None for an
    # owner_type=="admin" row -- there's no such concept for a GroupAdmin.
    member_id: Optional[UUID] = None
    group_admin_id: Optional[UUID] = None
    owner_type: Literal["member", "admin"]
    # Whether this row belongs to the admin currently viewing the list --
    # the signal for showing a self-service "Pay now" action on it.
    is_mine: bool = False
    name: str
    member_id_number: Optional[str] = None
    status: str
    # Display-layer only -- see compute_display_status's docstring. status
    # above stays the real, unmassaged value.
    display_status: str
    # Money is always a string on the wire (never a bare JSON number, to
    # avoid float precision loss) -- see known-limitations.md.
    amount_received: str
    paid_at: Optional[datetime] = None


class MemberVisibleContributionItem(BaseModel):
    """The full-transparency view any member of a purse's group can see --
    deliberately thinner than ContributionListItem (the admin dispute-
    resolution shape): name and status only, no amounts, no flag/manual-
    payment notes, no ids. owner_type is the one addition purely so the
    frontend can show an inline "(Admin)" label next to that row's name --
    same table, same columns, no separate section."""

    name: str
    owner_type: Literal["member", "admin"]
    status: str
    display_status: str


class PurseSummary(BaseModel):
    # Fully settled -- electronic (paid) and manually-logged (paid_manual)
    # combined, same convention as counts_for_purses' own paid_count.
    paid_count: int
    # display_status-driven: an expired invoice on a purse that's still
    # open counts here, not in expired_count -- see compute_display_status.
    pending_count: int
    expired_count: int
    flagged_count: int
    # Money is always a string on the wire (never a bare JSON number, to
    # avoid float precision loss) -- see known-limitations.md.
    total_collected: str
    # total_collected split by source -- real, Monnify-confirmed money
    # (collected_via_kontributa) vs. a rep's own offline/cash record
    # (collected_manually), which they always sum back to total_collected.
    collected_via_kontributa: str
    collected_manually: str
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
    # Money is always a string on the wire (never a bare JSON number, to
    # avoid float precision loss) -- see known-limitations.md.
    total_collected: str
    total_goal: Optional[str] = None
    progress_percent: Optional[int] = None
    # A derived pacing hint for the Group Admin dashboard's purses table --
    # "pending_close" (open, deadline passed), "lagging" (deadline within
    # LAGGING_DEADLINE_DAYS and completion below LAGGING_PERCENT_THRESHOLD),
    # "on_track" (any other open purse), or null for a closed purse. This is
    # a simple heuristic against deadline + completion, not true pacing --
    # comparing percent_complete against elapsed time since creation would
    # need the purse's created_at exposed, which it currently isn't.
    pacing_status: Optional[str] = None


class PurseListItemMemberOut(PurseOut):
    contribution_status: str


class PurseDetailAdminOut(PurseOut):
    enroll_mode: EnrollModeLiteral
    paid_count: int
    total_count: int


class PurseDetailMemberOut(PurseOut):
    enroll_mode: EnrollModeLiteral
    contribution_status: str
    # Display-layer only -- see compute_display_status's docstring.
    # contribution_status above stays the real, unmassaged value.
    display_status: str


class PurseUpdateResponse(BaseModel):
    id: UUID
    amount: str
    deadline: str


class PurseStatusResponse(BaseModel):
    id: UUID
    status: str


class AvailableBalanceOut(BaseModel):
    purse_id: UUID
    # "custodian" (the balance figures below apply) or "direct" (payments
    # go straight to the group's own account -- there is no held balance
    # concept, so the money fields are null rather than a misleading "0").
    settlement_mode: str
    collected_total: Optional[str] = None
    paid_out_total: Optional[str] = None
    available_balance: Optional[str] = None
