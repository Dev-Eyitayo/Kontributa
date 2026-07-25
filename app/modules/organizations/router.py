from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_admin_user
from app.core.db import get_db
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, Paginated
from app.core.response import StandardResponse, success_response
from app.modules.organizations.schemas import (
    AdminCreateGroupRequest,
    AdminCreateOrganizationRequest,
    AdminGroupDetailResponse,
    AdminGroupResponse,
    AdminMemberListItem,
    AdminMemberResponse,
    AdminOrganizationResponse,
    AdminUpdateGroupRequest,
    AdminUpdateMemberRequest,
    AdminUpdateOrganizationRequest,
    GroupOut,
    OrganizationOut,
    RemoveMemberResponse,
)
from app.modules.organizations.service import OrganizationService

public_router = APIRouter(tags=["organizations"])
admin_router = APIRouter(prefix="/admin", tags=["organizations-admin"])


def get_organization_service(db: AsyncSession = Depends(get_db)) -> OrganizationService:
    return OrganizationService(db)


@public_router.get("/organizations", response_model=StandardResponse[list[OrganizationOut]])
async def list_organizations(
    q: Optional[str] = Query(default=None),
    service: OrganizationService = Depends(get_organization_service),
) -> JSONResponse:
    orgs = await service.list_organizations(q)
    return success_response(
        [{"id": str(o.id), "name": o.name, "short_code": o.short_code} for o in orgs]
    )


@public_router.get("/organizations/{organization_id}/groups", response_model=StandardResponse[list[GroupOut]])
async def list_groups(
    organization_id: UUID,
    service: OrganizationService = Depends(get_organization_service),
) -> JSONResponse:
    groups = await service.list_groups(organization_id)
    return success_response(
        [{"id": str(g.id), "name": g.name, "short_code": g.short_code, "cohort": g.cohort} for g in groups]
    )


@admin_router.get("/organizations", response_model=StandardResponse[list[AdminOrganizationResponse]])
async def list_all_organizations(
    _: CurrentUser = Depends(get_current_admin_user),
    service: OrganizationService = Depends(get_organization_service),
) -> JSONResponse:
    orgs = await service.list_all_organizations()
    return success_response(
        [
            {
                "id": str(o.id),
                "name": o.name,
                "short_code": o.short_code,
                "org_type": o.org_type.value,
                "active": o.active,
                "member_id_format": o.member_id_format,
            }
            for o in orgs
        ]
    )


@admin_router.post("/organizations", status_code=201, response_model=StandardResponse[AdminOrganizationResponse])
async def create_organization(
    payload: AdminCreateOrganizationRequest,
    _: CurrentUser = Depends(get_current_admin_user),
    service: OrganizationService = Depends(get_organization_service),
) -> JSONResponse:
    org = await service.create_organization(payload)
    return success_response(
        {
            "id": str(org.id),
            "name": org.name,
            "short_code": org.short_code,
            "org_type": org.org_type.value,
            "active": org.active,
            "member_id_format": org.member_id_format,
        },
        status_code=201,
    )


@admin_router.patch("/organizations/{organization_id}", response_model=StandardResponse[AdminOrganizationResponse])
async def update_organization(
    organization_id: UUID,
    payload: AdminUpdateOrganizationRequest,
    _: CurrentUser = Depends(get_current_admin_user),
    service: OrganizationService = Depends(get_organization_service),
) -> JSONResponse:
    org = await service.update_organization(organization_id, payload)
    return success_response(
        {
            "id": str(org.id),
            "name": org.name,
            "short_code": org.short_code,
            "org_type": org.org_type.value,
            "active": org.active,
            "member_id_format": org.member_id_format,
        }
    )


@admin_router.post("/groups", status_code=201, response_model=StandardResponse[AdminGroupResponse])
async def create_group(
    payload: AdminCreateGroupRequest,
    _: CurrentUser = Depends(get_current_admin_user),
    service: OrganizationService = Depends(get_organization_service),
) -> JSONResponse:
    group = await service.create_group(payload)
    return success_response(
        {
            "id": str(group.id),
            "organization_id": str(group.organization_id),
            "name": group.name,
            "short_code": group.short_code,
        },
        status_code=201,
    )


@admin_router.patch("/groups/{group_id}", response_model=StandardResponse[AdminGroupDetailResponse])
async def update_group(
    group_id: UUID,
    payload: AdminUpdateGroupRequest,
    current_user: CurrentUser = Depends(get_current_admin_user),
    service: OrganizationService = Depends(get_organization_service),
) -> JSONResponse:
    group = await service.update_group(group_id, payload, current_user.id)
    return success_response(
        {
            "id": str(group.id),
            "organization_id": str(group.organization_id),
            "name": group.name,
            "short_code": group.short_code,
            "cohort": group.cohort,
        }
    )


@admin_router.get("/groups/{group_id}/members", response_model=StandardResponse[Paginated[AdminMemberListItem]])
async def list_group_members(
    group_id: UUID,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    _: CurrentUser = Depends(get_current_admin_user),
    service: OrganizationService = Depends(get_organization_service),
) -> JSONResponse:
    rows, total = await service.list_group_members(group_id, limit, offset)
    return success_response(
        {
            "items": [
                {
                    "id": str(member.id),
                    "name": f"{user.first_name} {user.last_name}",
                    "member_id_number": member.member_id_number,
                    "cohort": member.cohort,
                    "invite_source": (
                        {"token_suffix": invite.token[-8:], "created_at": invite.created_at.isoformat()}
                        if invite is not None
                        else None
                    ),
                    "joined_at": member.created_at.isoformat(),
                }
                for member, user, invite in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


@admin_router.patch("/members/{member_id}", response_model=StandardResponse[AdminMemberResponse])
async def update_member(
    member_id: UUID,
    payload: AdminUpdateMemberRequest,
    current_user: CurrentUser = Depends(get_current_admin_user),
    service: OrganizationService = Depends(get_organization_service),
) -> JSONResponse:
    member, user = await service.update_member(member_id, payload, current_user.id)
    return success_response(
        {
            "id": str(member.id),
            "name": f"{user.first_name} {user.last_name}",
            "member_id_number": member.member_id_number,
        }
    )


@admin_router.delete("/members/{member_id}", response_model=StandardResponse[RemoveMemberResponse])
async def remove_member(
    member_id: UUID,
    current_user: CurrentUser = Depends(get_current_admin_user),
    service: OrganizationService = Depends(get_organization_service),
) -> JSONResponse:
    await service.remove_member(member_id, current_user.id)
    return success_response({"removed": True})
