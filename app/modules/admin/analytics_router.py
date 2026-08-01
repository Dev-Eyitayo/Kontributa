from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_admin_user
from app.core.db import get_db
from app.core.exceptions import ValidationAppError
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, Paginated
from app.core.response import StandardResponse, success_response
from app.modules.admin.analytics_schemas import (
    AnalyticsOverviewResponse,
    GroupHealthItem,
    GrowthTrendPoint,
    NeedsAttentionResponse,
    ProviderSplitItem,
    RevenueTrendPoint,
)
from app.modules.admin.analytics_service import AdminAnalyticsService

router = APIRouter(prefix="/admin/analytics", tags=["admin-analytics"])

# The page's period selector only ever offers these three. Plain `int`
# rather than `Literal[7, 30, 90]` on the Query param -- FastAPI/Pydantic
# v2 does not reliably coerce a query string ("30") against an int
# Literal, so that annotation 422s on every request regardless of value.
# Validated by hand in _validate_days() instead, with the same effect.
_ALLOWED_DAYS = (7, 30, 90)


def _validate_days(days: int) -> int:
    if days not in _ALLOWED_DAYS:
        raise ValidationAppError(f"days must be one of {_ALLOWED_DAYS}")
    return days


def get_analytics_service(db: AsyncSession = Depends(get_db)) -> AdminAnalyticsService:
    return AdminAnalyticsService(db)


@router.get("/overview", response_model=StandardResponse[AnalyticsOverviewResponse])
async def get_overview(
    days: int = Query(default=30),
    _: CurrentUser = Depends(get_current_admin_user),
    service: AdminAnalyticsService = Depends(get_analytics_service),
) -> JSONResponse:
    return success_response(await service.get_overview(_validate_days(days)))


@router.get("/revenue-trend", response_model=StandardResponse[list[RevenueTrendPoint]])
async def get_revenue_trend(
    days: int = Query(default=30),
    _: CurrentUser = Depends(get_current_admin_user),
    service: AdminAnalyticsService = Depends(get_analytics_service),
) -> JSONResponse:
    return success_response(await service.get_revenue_trend(_validate_days(days)))


@router.get("/growth-trend", response_model=StandardResponse[list[GrowthTrendPoint]])
async def get_growth_trend(
    days: int = Query(default=30),
    _: CurrentUser = Depends(get_current_admin_user),
    service: AdminAnalyticsService = Depends(get_analytics_service),
) -> JSONResponse:
    return success_response(await service.get_growth_trend(_validate_days(days)))


@router.get("/needs-attention", response_model=StandardResponse[NeedsAttentionResponse])
async def get_needs_attention(
    _: CurrentUser = Depends(get_current_admin_user),
    service: AdminAnalyticsService = Depends(get_analytics_service),
) -> JSONResponse:
    return success_response(await service.get_needs_attention())


@router.get("/provider-split", response_model=StandardResponse[list[ProviderSplitItem]])
async def get_provider_split(
    days: int = Query(default=30),
    _: CurrentUser = Depends(get_current_admin_user),
    service: AdminAnalyticsService = Depends(get_analytics_service),
) -> JSONResponse:
    return success_response(await service.get_provider_split(_validate_days(days)))


@router.get("/groups-health", response_model=StandardResponse[Paginated[GroupHealthItem]])
async def get_groups_health(
    status: Literal["active", "dormant", "new"] = Query(...),
    days: int = Query(default=30),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    _: CurrentUser = Depends(get_current_admin_user),
    service: AdminAnalyticsService = Depends(get_analytics_service),
) -> JSONResponse:
    items, total = await service.list_groups_health(status, _validate_days(days), limit, offset)
    return success_response({"items": items, "total": total, "limit": limit, "offset": offset})
