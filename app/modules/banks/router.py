import json

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from app.core.auth import CurrentUser, get_current_group_admin_user
from app.core.redis import get_redis
from app.core.response import StandardResponse, success_response
from app.modules.banks.schemas import BankOut
from app.modules.payments.service import MonnifyClient, get_monnify_client

router = APIRouter(tags=["banks"])

CACHE_KEY = "banks:monnify"
CACHE_TTL_SECONDS = 24 * 60 * 60  # bank reference data changes rarely


@router.get("/banks", response_model=StandardResponse[list[BankOut]])
async def list_banks(
    _: CurrentUser = Depends(get_current_group_admin_user),
    redis: Redis = Depends(get_redis),
    monnify: MonnifyClient = Depends(get_monnify_client),
) -> JSONResponse:
    cached = await redis.get(CACHE_KEY)
    if cached is not None:
        return success_response(json.loads(cached))

    banks = await monnify.list_banks()
    await redis.set(CACHE_KEY, json.dumps(banks), ex=CACHE_TTL_SECONDS)
    return success_response(banks)
