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
