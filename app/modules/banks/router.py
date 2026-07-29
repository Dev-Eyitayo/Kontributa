import json

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from app.core.auth import CurrentUser, get_current_group_admin_user
from app.core.redis import get_redis
from app.core.response import StandardResponse, success_response
from app.modules.banks.schemas import BankOut
from app.modules.payments.service import get_payment_provider
from app.modules.platform_settings.router import get_platform_settings_service
from app.modules.platform_settings.service import PlatformSettingsService

router = APIRouter(tags=["banks"])

CACHE_TTL_SECONDS = 24 * 60 * 60  # bank reference data changes rarely


@router.get("/banks", response_model=StandardResponse[list[BankOut]])
async def list_banks(
    _: CurrentUser = Depends(get_current_group_admin_user),
    redis: Redis = Depends(get_redis),
    platform_settings: PlatformSettingsService = Depends(get_platform_settings_service),
) -> JSONResponse:
    settings_row = await platform_settings.get_or_create()
    provider_name = settings_row.active_payment_provider
    cache_key = f"banks:{provider_name}"

    cached = await redis.get(cache_key)
    if cached is not None:
        return success_response(json.loads(cached))

    provider = get_payment_provider(provider_name)
    banks = await provider.list_banks()
    await redis.set(cache_key, json.dumps(banks), ex=CACHE_TTL_SECONDS)
    return success_response(banks)
