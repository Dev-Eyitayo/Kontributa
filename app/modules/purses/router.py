from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import (
    CurrentUser,
    get_current_group_admin_user,
    get_current_member_user,
    get_current_user,
    require_verified_email,
)
from app.core.db import get_db
from app.core.exceptions import ForbiddenError
from app.core.idempotency import IdempotencyStore, fingerprint, get_idempotency_key, get_idempotency_store
from app.core.response import success_response
from app.modules.contributions.service import ContributionService
from app.modules.group_admins.service import GroupAdminService
from app.modules.members.service import MemberService
from app.modules.payouts.service import PayoutService
from app.modules.purses.schemas import CreatePurseRequest, UpdatePurseRequest
from app.modules.purses.service import PurseService

router = APIRouter(prefix="/purses", tags=["purses"])

IDEMPOTENCY_SCOPE_CREATE_PURSE = "purses:create"


def get_purse_service(db: AsyncSession = Depends(get_db)) -> PurseService:
    return PurseService(db)


def get_contribution_service(db: AsyncSession = Depends(get_db)) -> ContributionService:
    return ContributionService(db)


def get_group_admin_service(db: AsyncSession = Depends(get_db)) -> GroupAdminService:
    return GroupAdminService(db)


def get_member_service(db: AsyncSession = Depends(get_db)) -> MemberService:
    return MemberService(db)


def _purse_out(purse) -> dict:
    return {
        "id": str(purse.id),
        "title": purse.title,
        "amount": str(purse.amount),
        "deadline": purse.deadline.isoformat(),
        "status": purse.status.value,
    }


@router.post("", status_code=201)
async def create_purse(
    payload: CreatePurseRequest,
    idempotency_key: Optional[str] = Depends(get_idempotency_key),
    current_user: CurrentUser = Depends(get_current_group_admin_user),
    _verified: CurrentUser = Depends(require_verified_email),
    purse_service: PurseService = Depends(get_purse_service),
    admin_service: GroupAdminService = Depends(get_group_admin_service),
    idem_store: IdempotencyStore = Depends(get_idempotency_store),
) -> JSONResponse:
    admin = await admin_service.get_by_user_id(current_user.id)

    request_fingerprint = fingerprint(payload.model_dump(mode="json"))
    if idempotency_key is not None:
        cached = await idem_store.begin(
            IDEMPOTENCY_SCOPE_CREATE_PURSE, admin.id, idempotency_key, request_fingerprint
        )
        if cached is not None:
            return JSONResponse(status_code=cached["status_code"], content=cached["body"])

    try:
        purse = await purse_service.create(admin, payload)
    except Exception:
        if idempotency_key is not None:
            await idem_store.release(IDEMPOTENCY_SCOPE_CREATE_PURSE, admin.id, idempotency_key)
        raise

    envelope_body = {"success": True, "data": _purse_out(purse), "error": None}
    if idempotency_key is not None:
        await idem_store.complete(
            IDEMPOTENCY_SCOPE_CREATE_PURSE,
            admin.id,
            idempotency_key,
            request_fingerprint,
            201,
            envelope_body,
        )
    return JSONResponse(status_code=201, content=envelope_body)


@router.get("")
async def list_purses(
    status: Optional[str] = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    if current_user.role == "group_admin":
        admin = await GroupAdminService(db).get_by_user_id(current_user.id)
        purses = await PurseService(db).list_for_admin(admin, status)
        purse_ids = [p.id for p in purses]
        counts = await ContributionService(db).counts_for_purses(purse_ids)
        return success_response(
            [
                {
                    **_purse_out(p),
                    "paid_count": counts.get(p.id, (0, 0))[0],
                    "total_count": counts.get(p.id, (0, 0))[1],
                }
                for p in purses
            ]
        )

    member = await MemberService(db).get_by_user_id(current_user.id)
    rows = await PurseService(db).list_for_member(member, status)
    return success_response(
        [{**_purse_out(purse), "contribution_status": cstatus} for purse, cstatus in rows]
    )


@router.get("/{purse_id}")
async def get_purse(
    purse_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    purse = await PurseService(db).get_detail(purse_id)

    if current_user.role == "group_admin":
        admin = await GroupAdminService(db).get_by_user_id(current_user.id)
        if purse.group_id != admin.group_id:
            raise ForbiddenError("cannot view a purse outside your group")
        counts = await ContributionService(db).counts_for_purses([purse.id])
        paid_count, total_count = counts.get(purse.id, (0, 0))
        return success_response(
            {
                **_purse_out(purse),
                "enroll_mode": purse.enroll_mode.value,
                "paid_count": paid_count,
                "total_count": total_count,
            }
        )

    member = await MemberService(db).get_by_user_id(current_user.id)
    contribution = await ContributionService(db).get_for_member(purse.id, member.id)
    if contribution is None:
        raise ForbiddenError("not eligible for this purse")
    return success_response(
        {
            **_purse_out(purse),
            "enroll_mode": purse.enroll_mode.value,
            "contribution_status": contribution.status.value,
        }
    )


@router.patch("/{purse_id}")
async def update_purse(
    purse_id: UUID,
    payload: UpdatePurseRequest,
    current_user: CurrentUser = Depends(get_current_group_admin_user),
    purse_service: PurseService = Depends(get_purse_service),
    admin_service: GroupAdminService = Depends(get_group_admin_service),
) -> JSONResponse:
    admin = await admin_service.get_by_user_id(current_user.id)
    purse = await purse_service.update(admin, purse_id, payload)
    return success_response({"id": str(purse.id), "amount": str(purse.amount), "deadline": purse.deadline.isoformat()})


@router.post("/{purse_id}/close")
async def close_purse(
    purse_id: UUID,
    current_user: CurrentUser = Depends(get_current_group_admin_user),
    purse_service: PurseService = Depends(get_purse_service),
    admin_service: GroupAdminService = Depends(get_group_admin_service),
) -> JSONResponse:
    admin = await admin_service.get_by_user_id(current_user.id)
    purse = await purse_service.close(admin, purse_id)
    return success_response({"id": str(purse.id), "status": purse.status.value})


@router.get("/{purse_id}/contributions")
async def list_contributions(
    purse_id: UUID,
    status: Optional[str] = Query(default=None),
    current_user: CurrentUser = Depends(get_current_group_admin_user),
    purse_service: PurseService = Depends(get_purse_service),
    admin_service: GroupAdminService = Depends(get_group_admin_service),
    contribution_service: ContributionService = Depends(get_contribution_service),
) -> JSONResponse:
    admin = await admin_service.get_by_user_id(current_user.id)
    purse = await purse_service.get_by_id(purse_id)
    if purse.group_id != admin.group_id:
        raise ForbiddenError("cannot view contributions for a purse outside your group")

    rows = await contribution_service.list_for_purse(purse_id, status)
    return success_response(
        [
            {
                "member_id": str(member.id),
                "name": f"{user.first_name} {user.last_name}",
                "member_id_number": member.member_id_number,
                "status": contribution.status.value,
                "amount_received": str(contribution.amount_received),
                "paid_at": contribution.paid_at.isoformat() if contribution.paid_at else None,
            }
            for contribution, member, user in rows
        ]
    )


@router.get("/{purse_id}/summary")
async def get_summary(
    purse_id: UUID,
    current_user: CurrentUser = Depends(get_current_group_admin_user),
    purse_service: PurseService = Depends(get_purse_service),
    admin_service: GroupAdminService = Depends(get_group_admin_service),
    contribution_service: ContributionService = Depends(get_contribution_service),
) -> JSONResponse:
    admin = await admin_service.get_by_user_id(current_user.id)
    purse = await purse_service.get_by_id(purse_id)
    if purse.group_id != admin.group_id:
        raise ForbiddenError("cannot view summary for a purse outside your group")

    summary = await contribution_service.summary_for_purse(purse_id)
    return success_response({**summary, "total_collected": str(summary["total_collected"])})


@router.get("/{purse_id}/available-balance")
async def get_available_balance(
    purse_id: UUID,
    current_user: CurrentUser = Depends(get_current_group_admin_user),
    db: AsyncSession = Depends(get_db),
    purse_service: PurseService = Depends(get_purse_service),
    admin_service: GroupAdminService = Depends(get_group_admin_service),
) -> JSONResponse:
    admin = await admin_service.get_by_user_id(current_user.id)
    purse = await purse_service.get_by_id(purse_id)
    if purse.group_id != admin.group_id:
        raise ForbiddenError("cannot view available balance for a purse outside your group")

    balance = await PayoutService(db).get_available_balance(purse_id)
    return success_response(
        {
            "purse_id": str(balance["purse_id"]),
            "collected_total": str(balance["collected_total"]),
            "paid_out_total": str(balance["paid_out_total"]),
            "available_balance": str(balance["available_balance"]),
        }
    )
