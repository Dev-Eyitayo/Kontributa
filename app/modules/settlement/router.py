from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_group_admin_user
from app.core.db import get_db
from app.core.exceptions import ForbiddenError, NotFoundError
from app.core.response import success_response
from app.modules.group_admins.service import GroupAdminService
from app.modules.payments.service import MonnifyClient, get_monnify_client
from app.modules.settlement.schemas import SettlementLookupRequest, SettlementSaveRequest
from app.modules.settlement.service import SettlementService

router = APIRouter(prefix="/groups", tags=["settlement"])


def get_settlement_service(db: AsyncSession = Depends(get_db)) -> SettlementService:
    return SettlementService(db)


async def _assert_admin_of_group(db: AsyncSession, current_user: CurrentUser, group_id: UUID):
    admin = await GroupAdminService(db).get_by_user_id(current_user.id)
    if admin.group_id != group_id:
        raise ForbiddenError("cannot manage another group's settlement account")
    return admin


@router.post("/{group_id}/settlement-account/lookup")
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


@router.post("/{group_id}/settlement-account")
async def save_settlement_account(
    group_id: UUID,
    payload: SettlementSaveRequest,
    current_user: CurrentUser = Depends(get_current_group_admin_user),
    db: AsyncSession = Depends(get_db),
    monnify: MonnifyClient = Depends(get_monnify_client),
    service: SettlementService = Depends(get_settlement_service),
) -> JSONResponse:
    admin = await _assert_admin_of_group(db, current_user, group_id)
    account = await service.save(
        monnify, admin, payload.bank_code, payload.account_number, payload.confirmed_account_name
    )
    return success_response(
        {
            "id": str(account.id),
            "bank_name": account.bank_name,
            "account_number": account.account_number,
            "account_name_verified": account.account_name_verified,
            "verified_at": account.verified_at.isoformat() if account.verified_at else None,
        },
        status_code=201,
    )


@router.get("/{group_id}/settlement-account")
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

    return success_response(
        {
            "id": str(account.id),
            "bank_name": account.bank_name,
            "account_number": SettlementService.mask_account_number(account.account_number),
            "account_name_verified": account.account_name_verified,
            "verified_at": account.verified_at.isoformat() if account.verified_at else None,
        }
    )
