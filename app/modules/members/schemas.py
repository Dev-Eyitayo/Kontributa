from typing import Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class JoinRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    member_id_number: Optional[str] = None


class JoinResponse(BaseModel):
    id: UUID
    group_id: UUID
    cohort: Optional[str] = None
    verification_status: str


class JoinAdditionalGroupRequest(BaseModel):
    member_id_number: Optional[str] = None


class GroupBrief(BaseModel):
    id: UUID
    name: str
    short_code: str


class MyMemberGroupItem(BaseModel):
    id: UUID
    name: str
    short_code: str
    cohort: Optional[str] = None
    # Per (user, group), never global -- a member in two groups can have
    # two different id numbers on file.
    member_id_number: Optional[str] = None


class MemberMeResponse(BaseModel):
    id: UUID
    first_name: str
    last_name: str
    group: GroupBrief
    cohort: Optional[str] = None
    # Email-verification state, from User.is_verified -- distinct from
    # verification_status below (a separate, currently-unused per-member
    # identity flag that never actually transitions off "pending"). Any
    # screen asking "has this person verified their email" must read this
    # field, not verification_status.
    is_verified: bool
    verification_status: str
    member_id_number: Optional[str] = None


class MemberUpdateRequest(BaseModel):
    first_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    last_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    member_id_number: Optional[str] = None


class MemberUpdateResponse(BaseModel):
    id: UUID
    first_name: str
    last_name: str
    member_id_number: Optional[str] = None


class MemberPurseListItem(BaseModel):
    contribution_id: UUID
    purse_id: UUID
    title: str
    amount: str
    deadline: str
    contribution_status: str
    # Display-layer only -- an "expired" invoice on a purse that's still
    # open shows as "pending" here (the member can regenerate and pay any
    # time before the purse closes); contribution_status above stays the
    # real, unmassaged value. See compute_display_status's docstring.
    display_status: str
    # Which group this purse belongs to -- a member in more than one group
    # otherwise has no way to tell purses from different groups apart in a
    # single combined list.
    group: GroupBrief
