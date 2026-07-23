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


class GroupBrief(BaseModel):
    id: UUID
    name: str
    short_code: str


class MemberMeResponse(BaseModel):
    id: UUID
    first_name: str
    last_name: str
    group: GroupBrief
    cohort: Optional[str] = None
    verification_status: str


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
    purse_id: UUID
    title: str
    amount: str
    deadline: str
    contribution_status: str
