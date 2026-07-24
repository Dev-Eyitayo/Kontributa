from datetime import datetime, timezone
from typing import Optional, Union
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
from app.core.exceptions import BusinessRuleError, ForbiddenError
from app.core.idempotency import IdempotencyStore, fingerprint, get_idempotency_key, get_idempotency_store
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, Paginated
from app.core.response import StandardResponse, success_response
from app.modules.auth.models import User
from app.modules.contributions.service import ContributionService
from app.modules.group_admins.service import GroupAdminService
from app.modules.members.service import MemberService
from app.modules.payouts.service import PayoutService
from app.modules.purses.schemas import (
    AddMemberToPurseRequest,
    AddMemberToPurseResponse,
    AvailableBalanceOut,
    ContributionListItem,
    CreatePurseRequest,
    MemberVisibleContributionItem,
    PurseDetailAdminOut,
    PurseDetailMemberOut,
    PurseListItemAdminOut,
    PurseListItemMemberOut,
    PurseOut,
    PurseStatusResponse,
    PurseSummary,
    PurseUpdateResponse,
    UpdatePurseRequest,
)
from app.modules.purses.service import PurseService
from app.modules.settlement.models import SettlementMode
from app.modules.settlement.service import SettlementService

router = APIRouter(prefix="/purses", tags=["purses"])

IDEMPOTENCY_SCOPE_CREATE_PURSE = "purses:create"

# Thresholds for the Group Admin dashboard's derived "pacing_status" --
# see PurseListItemAdminOut's docstring for why this is a heuristic
# against deadline + completion rather than true elapsed-time pacing.
LAGGING_DEADLINE_DAYS = 5
LAGGING_PERCENT_THRESHOLD = 90.0


def _pacing_status(status: str, deadline: datetime, paid_count: int, total_count: int, now: datetime) -> Optional[str]:
    if status != "open":
        return None
    if deadline < now:
        return "pending_close"
    percent_complete = (paid_count / total_count * 100) if total_count else 0.0
    days_left = (deadline - now).total_seconds() / 86400
    if days_left <= LAGGING_DEADLINE_DAYS and percent_complete < LAGGING_PERCENT_THRESHOLD:
        return "lagging"
    return "on_track"


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


@router.post("", status_code=201, response_model=StandardResponse[PurseOut])
async def create_purse(
    payload: CreatePurseRequest,
    idempotency_key: Optional[str] = Depends(get_idempotency_key),
    current_user: CurrentUser = Depends(get_current_group_admin_user),
    _verified: CurrentUser = Depends(require_verified_email),
    purse_service: PurseService = Depends(get_purse_service),
    admin_service: GroupAdminService = Depends(get_group_admin_service),
    idem_store: IdempotencyStore = Depends(get_idempotency_store),
) -> JSONResponse:
    admin = await admin_service.get_admin_for_group(current_user.id, payload.group_id)

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


@router.get(
    "", response_model=StandardResponse[Union[Paginated[PurseListItemAdminOut], Paginated[PurseListItemMemberOut]]]
)
async def list_purses(
    group_id: Optional[UUID] = Query(default=None),
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    if current_user.role == "group_admin":
        if group_id is None:
            raise BusinessRuleError(
                "group_id is required -- an admin may manage more than one group", code="group_id_required"
            )
        admin = await GroupAdminService(db).get_admin_for_group(current_user.id, group_id)
        purses, total = await PurseService(db).list_for_admin(admin, status, limit, offset)
        purse_ids = [p.id for p in purses]
        contribution_service = ContributionService(db)
        counts = await contribution_service.counts_for_purses(purse_ids)
        collected = await contribution_service.collected_totals_for_purses(purse_ids)
        now = datetime.now(timezone.utc)
        items = []
        for p in purses:
            paid_count, total_count = counts.get(p.id, (0, 0))
            items.append(
                {
                    **_purse_out(p),
                    "paid_count": paid_count,
                    "total_count": total_count,
                    "total_collected": str(collected.get(p.id, 0)),
                    "pacing_status": _pacing_status(p.status.value, p.deadline, paid_count, total_count, now),
                }
            )
        return success_response(
            {
                "items": items,
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        )

    member = await MemberService(db).get_by_user_id(current_user.id)
    rows, total = await PurseService(db).list_for_member(member, status, limit, offset)
    return success_response(
        {
            "items": [{**_purse_out(purse), "contribution_status": cstatus} for purse, cstatus in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


@router.get("/{purse_id}", response_model=StandardResponse[Union[PurseDetailAdminOut, PurseDetailMemberOut]])
async def get_purse(
    purse_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    purse = await PurseService(db).get_detail(purse_id)

    if current_user.role == "group_admin":
        await GroupAdminService(db).get_admin_for_group(current_user.id, purse.group_id)
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

    # Resolved against *this* purse's own group (not get_by_user_id's
    # arbitrary pick among a multi-group member's several Member rows) --
    # otherwise a member viewing a purse from their second group could be
    # wrongly rejected if a different group's membership happened to be
    # picked instead.
    member = await MemberService(db).get_by_user_and_group(current_user.id, purse.group_id)
    contribution = await ContributionService(db).get_for_member(purse.id, member.id) if member else None
    if contribution is None:
        raise ForbiddenError("not eligible for this purse")
    return success_response(
        {
            **_purse_out(purse),
            "enroll_mode": purse.enroll_mode.value,
            "contribution_status": contribution.status.value,
        }
    )


@router.patch("/{purse_id}", response_model=StandardResponse[PurseUpdateResponse])
async def update_purse(
    purse_id: UUID,
    payload: UpdatePurseRequest,
    current_user: CurrentUser = Depends(get_current_group_admin_user),
    purse_service: PurseService = Depends(get_purse_service),
    admin_service: GroupAdminService = Depends(get_group_admin_service),
) -> JSONResponse:
    target = await purse_service.get_by_id(purse_id)
    admin = await admin_service.get_admin_for_group(current_user.id, target.group_id)
    purse = await purse_service.update(admin, purse_id, payload)
    return success_response({"id": str(purse.id), "amount": str(purse.amount), "deadline": purse.deadline.isoformat()})


@router.post("/{purse_id}/close", response_model=StandardResponse[PurseStatusResponse])
async def close_purse(
    purse_id: UUID,
    current_user: CurrentUser = Depends(get_current_group_admin_user),
    purse_service: PurseService = Depends(get_purse_service),
    admin_service: GroupAdminService = Depends(get_group_admin_service),
) -> JSONResponse:
    target = await purse_service.get_by_id(purse_id)
    admin = await admin_service.get_admin_for_group(current_user.id, target.group_id)
    purse = await purse_service.close(admin, purse_id)
    return success_response({"id": str(purse.id), "status": purse.status.value})


@router.get("/{purse_id}/contributions", response_model=StandardResponse[Paginated[ContributionListItem]])
async def list_contributions(
    purse_id: UUID,
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    current_user: CurrentUser = Depends(get_current_group_admin_user),
    db: AsyncSession = Depends(get_db),
    purse_service: PurseService = Depends(get_purse_service),
    admin_service: GroupAdminService = Depends(get_group_admin_service),
    contribution_service: ContributionService = Depends(get_contribution_service),
) -> JSONResponse:
    # A platform admin's JWT role claim is still "group_admin" (it's the
    # is_platform_admin flag on User that actually distinguishes them) --
    # and a platform admin has no GroupAdmin profile row, so check that
    # flag first rather than assuming a "group_admin"-role token always
    # has one. Mirrors audit/router.py::get_payout_audit's pattern.
    user_row = await db.get(User, current_user.id)
    purse = await purse_service.get_by_id(purse_id)
    if user_row is not None and user_row.is_platform_admin:
        pass
    else:
        await admin_service.get_admin_for_group(current_user.id, purse.group_id)

    rows, total = await contribution_service.list_for_purse(purse_id, status, limit, offset)
    return success_response(
        {
            "items": [
                {
                    "id": str(contribution.id),
                    "member_id": str(member.id),
                    "name": f"{user.first_name} {user.last_name}",
                    "member_id_number": member.member_id_number,
                    "status": contribution.status.value,
                    "amount_received": str(contribution.amount_received),
                    "paid_at": contribution.paid_at.isoformat() if contribution.paid_at else None,
                }
                for contribution, member, user in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


@router.get(
    "/{purse_id}/member-contributions", response_model=StandardResponse[Paginated[MemberVisibleContributionItem]]
)
async def list_contributions_for_member(
    purse_id: UUID,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    current_user: CurrentUser = Depends(get_current_member_user),
    purse_service: PurseService = Depends(get_purse_service),
    member_service: MemberService = Depends(get_member_service),
    contribution_service: ContributionService = Depends(get_contribution_service),
) -> JSONResponse:
    """Full transparency, minus admin-only resolution details -- any member
    of this purse's group can see who's paid on it, not just their own
    status. Scoped to group membership (not enrollment in this specific
    purse) -- 403 for anyone outside the group entirely."""
    purse = await purse_service.get_by_id(purse_id)
    member = await member_service.get_by_user_and_group(current_user.id, purse.group_id)
    if member is None:
        raise ForbiddenError("only a member of this purse's group can view its contribution statuses")

    rows, total = await contribution_service.list_for_purse(purse_id, None, limit, offset)
    return success_response(
        {
            "items": [
                {"name": f"{user.first_name} {user.last_name}", "status": contribution.status.value}
                for contribution, _member, user in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


@router.post("/{purse_id}/contributions", status_code=201, response_model=StandardResponse[AddMemberToPurseResponse])
async def add_member_to_purse(
    purse_id: UUID,
    payload: AddMemberToPurseRequest,
    current_user: CurrentUser = Depends(get_current_group_admin_user),
    purse_service: PurseService = Depends(get_purse_service),
    admin_service: GroupAdminService = Depends(get_group_admin_service),
) -> JSONResponse:
    target = await purse_service.get_by_id(purse_id)
    admin = await admin_service.get_admin_for_group(current_user.id, target.group_id)
    contribution = await purse_service.add_member(admin, purse_id, payload.member_id)
    return success_response(
        {
            "id": str(contribution.id),
            "purse_id": str(contribution.purse_id),
            "member_id": str(contribution.member_id),
            "status": contribution.status.value,
            "amount_expected": str(contribution.amount_expected),
        },
        status_code=201,
    )


@router.get("/{purse_id}/summary", response_model=StandardResponse[PurseSummary])
async def get_summary(
    purse_id: UUID,
    current_user: CurrentUser = Depends(get_current_group_admin_user),
    purse_service: PurseService = Depends(get_purse_service),
    admin_service: GroupAdminService = Depends(get_group_admin_service),
    contribution_service: ContributionService = Depends(get_contribution_service),
) -> JSONResponse:
    purse = await purse_service.get_by_id(purse_id)
    await admin_service.get_admin_for_group(current_user.id, purse.group_id)

    summary = await contribution_service.summary_for_purse(purse_id)
    return success_response({**summary, "total_collected": str(summary["total_collected"])})


@router.get("/{purse_id}/available-balance", response_model=StandardResponse[AvailableBalanceOut])
async def get_available_balance(
    purse_id: UUID,
    current_user: CurrentUser = Depends(get_current_group_admin_user),
    db: AsyncSession = Depends(get_db),
    purse_service: PurseService = Depends(get_purse_service),
    admin_service: GroupAdminService = Depends(get_group_admin_service),
) -> JSONResponse:
    purse = await purse_service.get_by_id(purse_id)
    await admin_service.get_admin_for_group(current_user.id, purse.group_id)

    settlement = await SettlementService(db).get(purse.group_id)
    if settlement is not None and settlement.settlement_mode == SettlementMode.DIRECT:
        # No held-balance concept applies -- payments already went straight
        # to the group's own account. A bare "0" here would look
        # indistinguishable from a custodian-mode purse that simply hasn't
        # collected anything yet, which is exactly the confusion this
        # distinct shape avoids.
        return success_response(
            {
                "purse_id": str(purse_id),
                "settlement_mode": "direct",
                "collected_total": None,
                "paid_out_total": None,
                "available_balance": None,
            }
        )

    balance = await PayoutService(db).get_available_balance(purse_id)
    return success_response(
        {
            "purse_id": str(balance["purse_id"]),
            "settlement_mode": "custodian",
            "collected_total": str(balance["collected_total"]),
            "paid_out_total": str(balance["paid_out_total"]),
            "available_balance": str(balance["available_balance"]),
        }
    )
