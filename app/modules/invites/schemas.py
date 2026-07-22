from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class OrganizationBrief(BaseModel):
    id: UUID
    name: str
    short_code: str


class GroupBrief(BaseModel):
    id: UUID
    name: str
    short_code: str


class InviteResolveResponse(BaseModel):
    group: GroupBrief
    cohort: Optional[str] = None
    organization: OrganizationBrief
    purse_title: Optional[str] = None


class InviteLinkCreateRequest(BaseModel):
    cohort: Optional[str] = None
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
