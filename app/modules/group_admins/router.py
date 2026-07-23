from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_group_admin_user
from app.core.db import get_db
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, Paginated
from app.core.response import StandardResponse, success_response
from app.modules.group_admins.schemas import (
    GroupAdminMeResponse,
    MemberListItem,
    OnboardGroupAdminRequest,
    OnboardGroupAdminResponse,
    RevokedResponse,
)
from app.modules.group_admins.service import GroupAdminService
from app.modules.invites.schemas import InviteLinkCreateRequest, InviteLinkCreateResponse, InviteLinkListItem
from app.modules.invites.service import InviteService

router = APIRouter(prefix="/group-admins", tags=["group-admins"])


def get_group_admin_service(db: AsyncSession = Depends(get_db)) -> GroupAdminService:
    return GroupAdminService(db)


@router.post("/onboard", status_code=201, response_model=StandardResponse[OnboardGroupAdminResponse])
async def onboard(
    payload: OnboardGroupAdminRequest,
    current_user: CurrentUser = Depends(get_current_group_admin_user),
    service: GroupAdminService = Depends(get_group_admin_service),
) -> JSONResponse:
    admin = await service.onboard(current_user.id, payload)
    return success_response(
        {
            "id": str(admin.id),
            "group_id": str(admin.group_id),
            "cohort": admin.cohort,
            "is_active_admin": admin.is_active_admin,
        },
        status_code=201,
    )


@router.get("/me", response_model=StandardResponse[GroupAdminMeResponse])
async def get_me(
    current_user: CurrentUser = Depends(get_current_group_admin_user),
    service: GroupAdminService = Depends(get_group_admin_service),
) -> JSONResponse:
    admin, user, group, members_count, purses_count = await service.get_me(current_user.id)
    return success_response(
        {
            "id": str(admin.id),
            "first_name": user.first_name,
            "last_name": user.last_name,
            "group": {"id": str(group.id), "name": group.name, "short_code": group.short_code},
            "cohort": admin.cohort,
            "purses_count": purses_count,
            "members_count": members_count,
        }
    )


@router.post("/invite-links", status_code=201, response_model=StandardResponse[InviteLinkCreateResponse])
async def create_invite_link(
    payload: InviteLinkCreateRequest,
    current_user: CurrentUser = Depends(get_current_group_admin_user),
    service: GroupAdminService = Depends(get_group_admin_service),
) -> JSONResponse:
    invite = await service.create_invite_link(current_user.id, payload)
    return success_response(
        {
            "id": str(invite.id),
            "token": invite.token,
            "url": InviteService.build_url(invite.token),
            "expires_at": invite.expires_at.isoformat(),
        },
        status_code=201,
    )


@router.get("/invite-links", response_model=StandardResponse[Paginated[InviteLinkListItem]])
async def list_invite_links(
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    current_user: CurrentUser = Depends(get_current_group_admin_user),
    service: GroupAdminService = Depends(get_group_admin_service),
) -> JSONResponse:
    invites, total = await service.list_invite_links(current_user.id, limit, offset)
    return success_response(
        {
            "items": [
                {
                    "id": str(i.id),
                    "url": InviteService.build_url(i.token),
                    "expires_at": i.expires_at.isoformat(),
                    "used_count": i.used_count,
                    "max_uses": i.max_uses,
                    "active": InviteService.is_active(i),
                }
                for i in invites
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


@router.delete("/invite-links/{invite_id}", response_model=StandardResponse[RevokedResponse])
async def revoke_invite_link(
    invite_id: UUID,
    current_user: CurrentUser = Depends(get_current_group_admin_user),
    service: GroupAdminService = Depends(get_group_admin_service),
) -> JSONResponse:
    await service.revoke_invite_link(current_user.id, invite_id)
    return success_response({"revoked": True})


@router.get("/members", response_model=StandardResponse[Paginated[MemberListItem]])
async def list_members(
    cohort: Optional[str] = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    current_user: CurrentUser = Depends(get_current_group_admin_user),
    service: GroupAdminService = Depends(get_group_admin_service),
) -> JSONResponse:
    rows, total = await service.list_members(current_user.id, cohort, limit, offset)
    return success_response(
        {
            "items": [
                {
                    "id": str(m.id),
                    "name": f"{u.first_name} {u.last_name}",
                    "member_id_number": m.member_id_number,
                    "cohort": m.cohort,
                    "invite_source": str(m.invite_source) if m.invite_source else None,
                    "joined_at": m.created_at.isoformat(),
                }
                for m, u in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )
