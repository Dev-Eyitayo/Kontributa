from typing import Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.auth import CurrentUser, get_current_admin_user, get_current_group_admin_user, get_current_user
from app.core.db import get_db
from app.core.exceptions import BusinessRuleError, ForbiddenError
from app.core.response import StandardResponse, success_response
from app.modules.auth.models import User
from app.modules.group_admins.service import GroupAdminService
from app.modules.notifications.service import SendByteClient, get_sendbyte_client
from app.modules.payments.service import MonnifyClient, get_monnify_client
from app.modules.payouts.models import Payout
from app.modules.payouts.schemas import (
    CreatePayoutRequest,
    PayoutApproveResponse,
    PayoutCreateResponse,
    PayoutDetailResponse,
    PayoutListItem,
    PayoutRejectResponse,
    RejectPayoutRequest,
)
from app.modules.payouts.service import PayoutService, initiate_transfer_for_payout

router = APIRouter(prefix="/payouts", tags=["payouts"])


def get_payout_service(db: AsyncSession = Depends(get_db)) -> PayoutService:
    return PayoutService(db)


def get_group_admin_service(db: AsyncSession = Depends(get_db)) -> GroupAdminService:
    return GroupAdminService(db)


def _payout_out(p: Payout) -> dict:
    return {
        "id": str(p.id),
        "group_id": str(p.group_id),
        "purse_id": str(p.purse_id) if p.purse_id else None,
        "amount": str(p.amount),
        "status": p.status.value,
        "requested_by": str(p.requested_by),
        "created_at": p.created_at.isoformat(),
    }


@router.post("", status_code=201, response_model=StandardResponse[PayoutCreateResponse])
async def create_payout(
    payload: CreatePayoutRequest,
    current_user: CurrentUser = Depends(get_current_group_admin_user),
    service: PayoutService = Depends(get_payout_service),
    admin_service: GroupAdminService = Depends(get_group_admin_service),
) -> JSONResponse:
    admin = await admin_service.get_admin_for_group(current_user.id, payload.group_id)
    payout = await service.create(admin, payload)
    return success_response(
        {"id": str(payout.id), "status": payout.status.value, "amount": str(payout.amount)}, status_code=201
    )


@router.get("", response_model=StandardResponse[list[PayoutListItem]])
async def list_payouts(
    group_id: Optional[UUID] = Query(default=None),
    status: Optional[str] = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    # A platform admin's JWT role claim is still "group_admin" -- it's the
    # is_platform_admin flag on User that actually distinguishes them (same
    # pattern as audit/router.py's get_payout_audit). Checking role alone
    # would either 404 a platform admin lacking a GroupAdmin profile, or
    # (worse) let a member fall through to list_all and see every
    # department's payouts -- both were live bugs before this check.
    service = PayoutService(db)
    user_row = await db.get(User, current_user.id)
    if user_row is not None and user_row.is_platform_admin:
        payouts = await service.list_all(status)
    elif current_user.role == "group_admin":
        if group_id is None:
            raise BusinessRuleError(
                "group_id is required -- an admin may manage more than one group", code="group_id_required"
            )
        await GroupAdminService(db).get_admin_for_group(current_user.id, group_id)
        payouts = await service.list_for_group(group_id, status)
    else:
        raise ForbiddenError("only a group admin or platform admin can list payouts")

    return success_response([_payout_out(p) for p in payouts])


@router.get("/{payout_id}", response_model=StandardResponse[PayoutDetailResponse])
async def get_payout(
    payout_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    service: PayoutService = Depends(get_payout_service),
) -> JSONResponse:
    payout = await service.get_by_id(payout_id)

    user_row = await db.get(User, current_user.id)
    if user_row is not None and user_row.is_platform_admin:
        pass
    elif current_user.role == "group_admin":
        await GroupAdminService(db).get_admin_for_group(current_user.id, payout.group_id)
    else:
        raise ForbiddenError("only a group admin or platform admin can view a payout")

    return success_response(
        {
            "id": str(payout.id),
            "status": payout.status.value,
            "amount": str(payout.amount),
            "monnify_transfer_ref": payout.monnify_transfer_ref,
            "failure_reason": payout.failure_reason,
        }
    )


@router.post("/{payout_id}/approve", response_model=StandardResponse[PayoutApproveResponse])
async def approve_payout(
    payout_id: UUID,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
    service: PayoutService = Depends(get_payout_service),
    monnify: MonnifyClient = Depends(get_monnify_client),
    sendbyte: SendByteClient = Depends(get_sendbyte_client),
) -> JSONResponse:
    payout = await service.get_by_id(payout_id)
    payout = await service.approve_only(payout, current_user.id)

    session_factory = async_sessionmaker(bind=db.bind, expire_on_commit=False)
    background_tasks.add_task(initiate_transfer_for_payout, payout.id, session_factory, monnify, sendbyte)

    return success_response(
        {"id": str(payout.id), "status": payout.status.value, "approved_by": str(payout.approved_by)}
    )


@router.post("/{payout_id}/reject", response_model=StandardResponse[PayoutRejectResponse])
async def reject_payout(
    payout_id: UUID,
    payload: RejectPayoutRequest,
    current_user: CurrentUser = Depends(get_current_admin_user),
    service: PayoutService = Depends(get_payout_service),
) -> JSONResponse:
    payout = await service.get_by_id(payout_id)
    payout = await service.reject(payout, current_user.id, payload.reason)
    return success_response(
        {"id": str(payout.id), "status": payout.status.value, "reason": payout.rejection_reason}
    )
