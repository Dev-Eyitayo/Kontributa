from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.response import StandardResponse, success_response
from app.modules.invites.schemas import InviteResolveResponse
from app.modules.invites.service import InviteService

router = APIRouter(prefix="/invites", tags=["invites"])


def get_invite_service(db: AsyncSession = Depends(get_db)) -> InviteService:
    return InviteService(db)


@router.get("/{token}", response_model=StandardResponse[InviteResolveResponse])
async def resolve_invite(token: str, service: InviteService = Depends(get_invite_service)) -> JSONResponse:
    invite, group, organization, purse_title = await service.resolve(token)
    return success_response(
        {
            "group": {"id": str(group.id), "name": group.name, "short_code": group.short_code},
            "cohort": invite.cohort,
            "organization": {
                "id": str(organization.id),
                "name": organization.name,
                "short_code": organization.short_code,
                "member_id_format": organization.member_id_format,
            },
            "purse_title": purse_title,
        }
    )
