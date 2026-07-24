from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_group_admin_user
from app.core.db import get_db
from app.core.exceptions import NotFoundError
from app.core.response import StandardResponse, success_response
from app.modules.group_admins.service import GroupAdminService
from app.modules.payments.service import MonnifyClient, get_monnify_client
from app.modules.payouts.service import PayoutService
from app.modules.platform_settings.service import PlatformSettingsService
from app.modules.settlement.models import SettlementAccount, SettlementMode
from app.modules.settlement.schemas import (
    SettlementAccountResponse,
    SettlementLookupRequest,
    SettlementLookupResponse,
    SettlementOptionsResponse,
    SettlementSaveRequest,
    SwitchSettlementModeRequest,
    SwitchSettlementModeResponse,
)
from app.modules.settlement.service import SettlementService

router = APIRouter(prefix="/groups", tags=["settlement"])


def get_settlement_service(db: AsyncSession = Depends(get_db)) -> SettlementService:
    return SettlementService(db)


def get_payout_service(db: AsyncSession = Depends(get_db)) -> PayoutService:
    return PayoutService(db)


def get_platform_settings_service(db: AsyncSession = Depends(get_db)) -> PlatformSettingsService:
    return PlatformSettingsService(db)


async def _assert_admin_of_group(db: AsyncSession, current_user: CurrentUser, group_id: UUID):
    return await GroupAdminService(db).get_admin_for_group(current_user.id, group_id)


def _account_out(account: SettlementAccount) -> dict:
    return {
        "id": str(account.id),
        "bank_name": account.bank_name,
        "account_number": SettlementService.mask_account_number(account.account_number),
        "account_name_verified": account.account_name_verified,
        "verified_at": account.verified_at.isoformat() if account.verified_at else None,
        "settlement_mode": account.settlement_mode.value,
        "direct_sub_account_code": account.direct_sub_account_code,
    }


@router.post("/{group_id}/settlement-account/lookup", response_model=StandardResponse[SettlementLookupResponse])
async def lookup_settlement_account(
    group_id: UUID,
    payload: SettlementLookupRequest,
    current_user: CurrentUser = Depends(get_current_group_admin_user),
    db: AsyncSession = Depends(get_db),
    monnify: MonnifyClient = Depends(get_monnify_client),
    service: SettlementService = Depends(get_settlement_service),
) -> JSONResponse:
    admin = await _assert_admin_of_group(db, current_user, group_id)
    result = await service.lookup(monnify, admin, payload.bank_code, payload.account_number)
    return success_response(result)


@router.get(
    "/{group_id}/settlement-options", response_model=StandardResponse[SettlementOptionsResponse]
)
async def get_settlement_options(
    group_id: UUID,
    current_user: CurrentUser = Depends(get_current_group_admin_user),
    db: AsyncSession = Depends(get_db),
    platform_settings: PlatformSettingsService = Depends(get_platform_settings_service),
) -> JSONResponse:
    # Read-only, group-admin-reachable signal for whether Custodian mode is
    # offered at all -- deliberately separate from /admin/settings (platform
    # admin only), which also holds platform_fee_percent that a group admin
    # has no business seeing.
    await _assert_admin_of_group(db, current_user, group_id)
    settings_row = await platform_settings.get_or_create()
    return success_response({"custodian_mode_enabled": settings_row.custodian_mode_enabled})


@router.post("/{group_id}/settlement-account", response_model=StandardResponse[SettlementAccountResponse])
async def save_settlement_account(
    group_id: UUID,
    payload: SettlementSaveRequest,
    current_user: CurrentUser = Depends(get_current_group_admin_user),
    db: AsyncSession = Depends(get_db),
    monnify: MonnifyClient = Depends(get_monnify_client),
    service: SettlementService = Depends(get_settlement_service),
    platform_settings: PlatformSettingsService = Depends(get_platform_settings_service),
) -> JSONResponse:
    # Custodian mode -- unchanged behavior from before Direct mode existed,
    # except it's now only reachable while custodian_mode_enabled is on.
    admin = await _assert_admin_of_group(db, current_user, group_id)
    account = await service.save(
        monnify, platform_settings, admin, payload.bank_code, payload.account_number, payload.confirmed_account_name
    )
    return success_response(_account_out(account), status_code=201)


@router.post(
    "/{group_id}/settlement-account/direct", response_model=StandardResponse[SettlementAccountResponse]
)
async def save_direct_settlement_account(
    group_id: UUID,
    payload: SettlementSaveRequest,
    current_user: CurrentUser = Depends(get_current_group_admin_user),
    db: AsyncSession = Depends(get_db),
    monnify: MonnifyClient = Depends(get_monnify_client),
    service: SettlementService = Depends(get_settlement_service),
) -> JSONResponse:
    admin = await _assert_admin_of_group(db, current_user, group_id)
    account = await service.save_direct(
        monnify, admin, payload.bank_code, payload.account_number, payload.confirmed_account_name
    )
    return success_response(_account_out(account), status_code=201)


@router.get("/{group_id}/settlement-account", response_model=StandardResponse[SettlementAccountResponse])
async def get_settlement_account(
    group_id: UUID,
    current_user: CurrentUser = Depends(get_current_group_admin_user),
    db: AsyncSession = Depends(get_db),
    service: SettlementService = Depends(get_settlement_service),
) -> JSONResponse:
    await _assert_admin_of_group(db, current_user, group_id)
    account = await service.get(group_id)
    if account is None:
        raise NotFoundError("no settlement account registered for this group")

    return success_response(_account_out(account))


@router.patch(
    "/{group_id}/settlement-account/mode", response_model=StandardResponse[SwitchSettlementModeResponse]
)
async def switch_settlement_mode(
    group_id: UUID,
    payload: SwitchSettlementModeRequest,
    current_user: CurrentUser = Depends(get_current_group_admin_user),
    db: AsyncSession = Depends(get_db),
    monnify: MonnifyClient = Depends(get_monnify_client),
    service: SettlementService = Depends(get_settlement_service),
    payout_service: PayoutService = Depends(get_payout_service),
    platform_settings: PlatformSettingsService = Depends(get_platform_settings_service),
) -> JSONResponse:
    admin = await _assert_admin_of_group(db, current_user, group_id)
    account = await service.switch_mode(
        monnify, payout_service, platform_settings, admin, SettlementMode(payload.new_mode)
    )
    return success_response({"settlement_mode": account.settlement_mode.value})
