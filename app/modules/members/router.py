from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import (
    CurrentUser,
    SingleUseTokenStore,
    get_current_member_user,
    get_current_user,
    get_email_verification_token_store,
)
from app.core.db import get_db
from app.core.response import StandardResponse, success_response
from app.modules.contributions.service import ContributionService
from app.modules.members.schemas import (
    JoinAdditionalGroupRequest,
    JoinRequest,
    JoinResponse,
    MemberMeResponse,
    MemberPurseListItem,
    MemberUpdateRequest,
    MemberUpdateResponse,
    MyMemberGroupItem,
)
from app.modules.members.service import MemberService
from app.modules.notifications.service import NotificationService, SendByteClient, get_sendbyte_client

router = APIRouter(prefix="/members", tags=["members"])


def get_member_service(
    db: AsyncSession = Depends(get_db),
    verify_email_tokens: SingleUseTokenStore = Depends(get_email_verification_token_store),
    sendbyte: SendByteClient = Depends(get_sendbyte_client),
) -> MemberService:
    return MemberService(db, verify_email_tokens, NotificationService(db, sendbyte))


def get_contribution_service(db: AsyncSession = Depends(get_db)) -> ContributionService:
    return ContributionService(db)


@router.post("/join/{token}", status_code=201, response_model=StandardResponse[JoinResponse])
async def join(
    token: str, payload: JoinRequest, service: MemberService = Depends(get_member_service)
) -> JSONResponse:
    member = await service.join(token, payload)
    return success_response(
        {
            "id": str(member.id),
            "group_id": str(member.group_id),
            "cohort": member.cohort,
            "verification_status": member.verification_status.value,
        },
        status_code=201,
    )


@router.post("/join-additional/{token}", status_code=201, response_model=StandardResponse[JoinResponse])
async def join_additional_group(
    token: str,
    payload: JoinAdditionalGroupRequest,
    current_user: CurrentUser = Depends(get_current_user),
    service: MemberService = Depends(get_member_service),
) -> JSONResponse:
    member = await service.join_additional_group(token, current_user.id, payload.member_id_number)
    return success_response(
        {
            "id": str(member.id),
            "group_id": str(member.group_id),
            "cohort": member.cohort,
            "verification_status": member.verification_status.value,
        },
        status_code=201,
    )


@router.get("/me/groups", response_model=StandardResponse[list[MyMemberGroupItem]])
async def list_my_groups(
    current_user: CurrentUser = Depends(get_current_member_user),
    service: MemberService = Depends(get_member_service),
) -> JSONResponse:
    groups = await service.list_my_groups(current_user.id)
    return success_response(
        [
            {
                "id": str(g["id"]),
                "name": g["name"],
                "short_code": g["short_code"],
                "organization_id": str(g["organization_id"]),
                "organization_name": g["organization_name"],
                "cohort": g["cohort"],
                "member_id_number": g["member_id_number"],
            }
            for g in groups
        ]
    )


@router.get("/me", response_model=StandardResponse[MemberMeResponse])
async def get_me(
    group_id: Optional[UUID] = Query(default=None),
    current_user: CurrentUser = Depends(get_current_member_user),
    service: MemberService = Depends(get_member_service),
) -> JSONResponse:
    member, user, group = await service.get_me(current_user.id, group_id)
    return success_response(
        {
            "id": str(member.id),
            "first_name": user.first_name,
            "last_name": user.last_name,
            "group": {"id": str(group.id), "name": group.name, "short_code": group.short_code},
            "cohort": member.cohort,
            "verification_status": member.verification_status.value,
            "member_id_number": member.member_id_number,
        }
    )


@router.patch("/me", response_model=StandardResponse[MemberUpdateResponse])
async def update_me(
    payload: MemberUpdateRequest,
    group_id: Optional[UUID] = Query(default=None),
    current_user: CurrentUser = Depends(get_current_member_user),
    service: MemberService = Depends(get_member_service),
) -> JSONResponse:
    member, user = await service.update_me(current_user.id, payload, group_id)
    return success_response(
        {
            "id": str(member.id),
            "first_name": user.first_name,
            "last_name": user.last_name,
            "member_id_number": member.member_id_number,
        }
    )


@router.get("/me/purses", response_model=StandardResponse[list[MemberPurseListItem]])
async def list_my_purses(
    current_user: CurrentUser = Depends(get_current_member_user),
    contribution_service: ContributionService = Depends(get_contribution_service),
) -> JSONResponse:
    # Spans every group this user is a Member of, not just one -- see
    # ContributionService.list_purses_for_user's docstring.
    rows = await contribution_service.list_purses_for_user(current_user.id)
    return success_response(
        [
            {
                "contribution_id": str(contribution.id),
                "purse_id": str(purse.id),
                "title": purse.title,
                "amount": str(purse.amount),
                "deadline": purse.deadline.isoformat(),
                "contribution_status": contribution.status.value,
                "group": {"id": str(group.id), "name": group.name, "short_code": group.short_code},
            }
            for contribution, purse, group in rows
        ]
    )
