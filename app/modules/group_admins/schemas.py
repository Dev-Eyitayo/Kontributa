from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class OnboardGroupAdminRequest(BaseModel):
    # Always creates a brand-new Group -- there is deliberately no way to
    # pass an existing group_id here. Letting a new admin pick and take
    # control of an existing group would be a privilege-escalation bug;
    # see known-limitations.md.
    organization_id: UUID
    new_group_name: str
    new_group_short_code: Optional[str] = None
    cohort: Optional[str] = None


class OnboardGroupAdminResponse(BaseModel):
    id: UUID
    group_id: UUID
    group_name: str
    group_short_code: str
    cohort: Optional[str] = None
    is_active_admin: bool


class GroupBrief(BaseModel):
    id: UUID
    name: str
    short_code: str


class MyGroupListItem(BaseModel):
    id: UUID
    name: str
    short_code: str
    organization_id: UUID
    organization_name: str
    cohort: Optional[str] = None
    members_count: int
    purses_count: int


class GroupAdminMeResponse(BaseModel):
    id: UUID
    first_name: str
    last_name: str
    group: GroupBrief
    cohort: Optional[str] = None
    purses_count: int
    members_count: int


class InviteSourceSummary(BaseModel):
    # A short, readable slice of the token rather than the raw invite_links
    # id -- an admin can actually recognize/cross-reference this against
    # the Invite Links screen, unlike a bare foreign key.
    token_suffix: str
    created_at: datetime


class MemberListItem(BaseModel):
    id: UUID
    name: str
    member_id_number: Optional[str] = None
    cohort: Optional[str] = None
    invite_source: Optional[InviteSourceSummary] = None
    joined_at: datetime


class RevokedResponse(BaseModel):
    revoked: bool = True
