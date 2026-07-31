from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

OrgType = Literal["school", "company", "association", "other"]


class OrganizationOut(BaseModel):
    id: UUID
    name: str
    short_code: str


class GroupOut(BaseModel):
    id: UUID
    name: str
    short_code: str
    cohort: Optional[str] = None


class AdminCreateOrganizationRequest(BaseModel):
    name: str
    short_code: str = Field(min_length=1, max_length=50)
    org_type: OrgType
    member_id_format: Optional[str] = None


class AdminOrganizationResponse(BaseModel):
    id: UUID
    name: str
    short_code: str
    org_type: str
    active: bool
    member_id_format: Optional[str] = None


class AdminUpdateOrganizationRequest(BaseModel):
    name: Optional[str] = None
    short_code: Optional[str] = None
    active: Optional[bool] = None
    member_id_format: Optional[str] = None


class AdminCreateGroupRequest(BaseModel):
    organization_id: UUID
    name: str
    short_code: str = Field(min_length=1, max_length=50)


class AdminGroupResponse(BaseModel):
    id: UUID
    organization_id: UUID
    name: str
    short_code: str


class AdminUpdateGroupRequest(BaseModel):
    name: Optional[str] = None
    short_code: Optional[str] = None
    cohort: Optional[str] = None


class AdminGroupDetailResponse(BaseModel):
    id: UUID
    organization_id: UUID
    name: str
    short_code: str
    cohort: Optional[str] = None


class AdminInviteSourceSummary(BaseModel):
    token_suffix: str
    created_at: datetime


class AdminMemberListItem(BaseModel):
    id: UUID
    name: str
    member_id_number: Optional[str] = None
    cohort: Optional[str] = None
    invite_source: Optional[AdminInviteSourceSummary] = None
    joined_at: datetime


class AdminUpdateMemberRequest(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    member_id_number: Optional[str] = None


class AdminMemberResponse(BaseModel):
    id: UUID
    name: str
    member_id_number: Optional[str] = None


class RemoveMemberResponse(BaseModel):
    removed: bool = True


class AdminGroupAdminInfo(BaseModel):
    user_id: UUID
    first_name: str
    last_name: str
    email: str


class AdminGroupListItem(BaseModel):
    id: UUID
    name: str
    short_code: str
    cohort: Optional[str] = None
    created_at: datetime
    admin: Optional[AdminGroupAdminInfo] = None
    members_count: int = 0
    purses_count: int = 0


class AdminGroupSettlementDetailResponse(BaseModel):
    id: UUID
    bank_name: str
    account_number: str
    account_name_verified: bool
    verified_at: Optional[datetime] = None
    settlement_mode: str
    direct_sub_account_code: Optional[str] = None
    payment_provider: str = "paystack"


class AdminGroupFullDetailResponse(BaseModel):
    id: UUID
    name: str
    short_code: str
    cohort: Optional[str] = None
    created_at: datetime
    admin: Optional[AdminGroupAdminInfo] = None
    members_count: int = 0
    purses_count: int = 0
    settlement_account: Optional[AdminGroupSettlementDetailResponse] = None


class AdminSettlementListItem(BaseModel):
    id: UUID
    settlement_reference: str
    sub_account_code: str
    group_id: Optional[UUID] = None
    group_name: Optional[str] = None
    amount: str
    fee: str
    settled_amount: str
    destination_account_name: Optional[str] = None
    destination_account_number: Optional[str] = None
    destination_bank_code: Optional[str] = None
    destination_bank_name: Optional[str] = None
    status: str
    settlement_time: Optional[datetime] = None
    created_at: datetime


