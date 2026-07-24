from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_admin_user
from app.core.db import get_db
from app.core.response import StandardResponse, success_response
from app.modules.platform_settings.schemas import PlatformSettingsResponse, UpdatePlatformSettingsRequest
from app.modules.platform_settings.service import PlatformSettingsService

router = APIRouter(prefix="/admin", tags=["platform-settings"])


def get_platform_settings_service(db: AsyncSession = Depends(get_db)) -> PlatformSettingsService:
    return PlatformSettingsService(db)


def _settings_out(row) -> dict:
    return {
        "custodian_mode_enabled": row.custodian_mode_enabled,
        "platform_fee_percent": str(row.platform_fee_percent),
    }


@router.get("/settings", response_model=StandardResponse[PlatformSettingsResponse])
async def get_settings(
    _: CurrentUser = Depends(get_current_admin_user),
    service: PlatformSettingsService = Depends(get_platform_settings_service),
) -> JSONResponse:
    row = await service.get_or_create()
    return success_response(_settings_out(row))


@router.patch("/settings", response_model=StandardResponse[PlatformSettingsResponse])
async def update_settings(
    payload: UpdatePlatformSettingsRequest,
    _: CurrentUser = Depends(get_current_admin_user),
    service: PlatformSettingsService = Depends(get_platform_settings_service),
) -> JSONResponse:
    row = await service.update(payload)
    return success_response(_settings_out(row))
