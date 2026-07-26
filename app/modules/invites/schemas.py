from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class OrganizationBrief(BaseModel):
    id: UUID
    name: str
    short_code: str
    # Only exposed here (invite resolution), not on the public org list/admin
    # endpoints -- a frontend needs this to hint the expected member_id_number
    # format on the join form itself, before the member ever submits it.
    member_id_format: Optional[str] = None


class GroupBrief(BaseModel):
    id: UUID
    name: str
    short_code: str


class InviteResolveResponse(BaseModel):
    group: GroupBrief
    cohort: Optional[str] = None
    # None for the vast majority of groups now -- Organization is a
    # Platform-Admin-only concept from the Member side (see
    # Group.organization_id). Still surfaced when present purely so the
    # join form can hint the expected member_id_number format up front.
    organization: Optional[OrganizationBrief] = None
    purse_title: Optional[str] = None


class InviteLinkCreateRequest(BaseModel):
    purse_id: Optional[UUID] = None
    expires_in_days: int
    max_uses: Optional[int] = None


class InviteLinkCreateResponse(BaseModel):
    id: UUID
    token: str
    url: str
    expires_at: datetime


class InviteLinkListItem(BaseModel):
    id: UUID
    url: str
    expires_at: datetime
    used_count: int
    max_uses: Optional[int] = None
    active: bool
