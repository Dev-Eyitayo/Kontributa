from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import (
    CurrentUser,
    SingleUseTokenStore,
    get_current_member_user,
    get_email_verification_token_store,
)
from app.core.db import get_db
from app.core.response import StandardResponse, success_response
from app.modules.contributions.service import ContributionService
from app.modules.members.schemas import (
    JoinRequest,
    JoinResponse,
    MemberMeResponse,
    MemberPurseListItem,
    MemberUpdateRequest,
    MemberUpdateResponse,
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


@router.get("/me", response_model=StandardResponse[MemberMeResponse])
async def get_me(
    current_user: CurrentUser = Depends(get_current_member_user),
    service: MemberService = Depends(get_member_service),
) -> JSONResponse:
    member, user, group = await service.get_me(current_user.id)
    return success_response(
        {
            "id": str(member.id),
            "first_name": user.first_name,
            "last_name": user.last_name,
            "group": {"id": str(group.id), "name": group.name, "short_code": group.short_code},
            "cohort": member.cohort,
            "verification_status": member.verification_status.value,
        }
    )


@router.patch("/me", response_model=StandardResponse[MemberUpdateResponse])
async def update_me(
    payload: MemberUpdateRequest,
    current_user: CurrentUser = Depends(get_current_member_user),
    service: MemberService = Depends(get_member_service),
) -> JSONResponse:
    member, user = await service.update_me(current_user.id, payload)
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
    member_service: MemberService = Depends(get_member_service),
    contribution_service: ContributionService = Depends(get_contribution_service),
) -> JSONResponse:
    member = await member_service.get_by_user_id(current_user.id)
    rows = await contribution_service.list_member_purses(member.id)
    return success_response(
        [
            {
                "purse_id": str(purse.id),
                "title": purse.title,
                "amount": str(purse.amount),
                "deadline": purse.deadline.isoformat(),
                "contribution_status": contribution.status.value,
            }
            for contribution, purse in rows
        ]
    )
