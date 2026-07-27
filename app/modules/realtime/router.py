from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.db import get_db
from app.core.exceptions import BusinessRuleError
from app.core.response import StandardResponse, success_response
from app.modules.contributions.service import ContributionService
from app.modules.realtime.schemas import RealtimeTokenResponse
from app.modules.realtime.service import RealtimeService, contribution_channel_name, get_realtime_service

router = APIRouter(prefix="/realtime", tags=["realtime"])


@router.get("/token", response_model=StandardResponse[RealtimeTokenResponse])
async def get_realtime_token(
    contribution_id: UUID = Query(...),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    service: RealtimeService = Depends(get_realtime_service),
) -> JSONResponse:
    """Scoped strictly to the caller's own contribution -- resolved and
    authorized server-side exactly like GET /contributions/{id}, never
    trusted from the client. Whoever has no business seeing this
    contribution gets a 403 before any token material is ever generated,
    same as if they'd tried the REST endpoint directly. Works identically
    for a Member's own contribution and a GroupAdmin's own contribution
    to their group's purse -- see ContributionService.resolve_and_assert_owner."""
    if not service.configured:
        raise BusinessRuleError("realtime updates are not configured", code="realtime_not_configured")

    contribution_service = ContributionService(db)
    contribution = await contribution_service.get_by_id(contribution_id)
    await contribution_service.resolve_and_assert_owner(current_user.id, contribution)

    token_request = service.create_token_request(
        capability={contribution_channel_name(contribution.id): ["subscribe"]},
        client_id=str(current_user.id),
    )
    return success_response(token_request)
