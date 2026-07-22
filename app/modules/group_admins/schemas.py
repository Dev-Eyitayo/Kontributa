from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class OnboardGroupAdminRequest(BaseModel):
    organization_id: UUID
    group_id: UUID
    cohort: Optional[str] = None


class OnboardGroupAdminResponse(BaseModel):
    id: UUID
    group_id: UUID
    cohort: Optional[str] = None
    is_active_admin: bool


class GroupBrief(BaseModel):
    id: UUID
    name: str
    short_code: str


class GroupAdminMeResponse(BaseModel):
    id: UUID
    first_name: str
    last_name: str
    group: GroupBrief
    cohort: Optional[str] = None
    purses_count: int
    members_count: int


class MemberListItem(BaseModel):
    id: UUID
    name: str
    member_id_number: Optional[str] = None
    cohort: Optional[str] = None
    invite_source: Optional[UUID] = None
    joined_at: datetime
