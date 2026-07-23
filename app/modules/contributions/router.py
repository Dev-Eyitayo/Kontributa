from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.db import get_db
from app.core.exceptions import ForbiddenError
from app.core.idempotency import IdempotencyStore, fingerprint, get_idempotency_key, get_idempotency_store
from app.core.response import StandardResponse, success_response
from app.modules.auth.models import User
from app.modules.contributions.models import Contribution
from app.modules.contributions.schemas import (
    ContributionDetailResponse,
    ContributionHistoryItem,
    GenerateInvoiceResponse,
    MarkManualRequest,
    MarkManualResponse,
    ResolveFlagRequest,
    ResolveFlagResponse,
)
from app.modules.contributions.service import ContributionService
from app.modules.group_admins.service import GroupAdminService
from app.modules.members.models import Member
from app.modules.members.service import MemberService
from app.modules.notifications.service import NotificationService, SendByteClient, get_sendbyte_client
from app.modules.payments.service import MonnifyClient, get_monnify_client
from app.modules.purses.models import Purse

router = APIRouter(prefix="/contributions", tags=["contributions"])

IDEMPOTENCY_SCOPE_MARK_MANUAL = "contributions:mark-manual"


def get_contribution_service(db: AsyncSession = Depends(get_db)) -> ContributionService:
    return ContributionService(db)


def _contribution_out(c: Contribution) -> dict:
    return {
        "id": str(c.id),
        "purse_id": str(c.purse_id),
        "member_id": str(c.member_id),
        "status": c.status.value,
        "amount_expected": str(c.amount_expected),
        "amount_received": str(c.amount_received),
        "account_number": c.account_number,
        "invoice_expires_at": c.invoice_expires_at.isoformat() if c.invoice_expires_at else None,
    }


async def _assert_member_owns(db: AsyncSession, current_user: CurrentUser, contribution: Contribution) -> Member:
    member = await MemberService(db).get_by_user_id(current_user.id)
    if contribution.member_id != member.id:
        raise ForbiddenError("cannot access another member's contribution")
    return member


async def _assert_admin_owns(db: AsyncSession, current_user: CurrentUser, contribution: Contribution):
    admin = await GroupAdminService(db).get_by_user_id(current_user.id)
    purse = await db.get(Purse, contribution.purse_id)
    if purse is None or purse.group_id != admin.group_id:
        raise ForbiddenError("cannot access a contribution outside your own group's purses")
    return admin, purse


@router.get("/{contribution_id}", response_model=StandardResponse[ContributionDetailResponse])
async def get_contribution(
    contribution_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    service: ContributionService = Depends(get_contribution_service),
) -> JSONResponse:
    contribution = await service.get_by_id(contribution_id)

    if current_user.role == "group_admin":
        await _assert_admin_owns(db, current_user, contribution)
    else:
        await _assert_member_owns(db, current_user, contribution)

    return success_response(_contribution_out(contribution))


@router.post("/{contribution_id}/generate-invoice", response_model=StandardResponse[GenerateInvoiceResponse])
async def generate_invoice(
    contribution_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    service: ContributionService = Depends(get_contribution_service),
    monnify: MonnifyClient = Depends(get_monnify_client),
    sendbyte: SendByteClient = Depends(get_sendbyte_client),
) -> JSONResponse:
    if current_user.role != "member":
        raise ForbiddenError("only a member can generate an invoice for their own contribution")

    contribution = await service.get_by_id(contribution_id)
    member = await _assert_member_owns(db, current_user, contribution)
    member_user = await db.get(User, member.user_id)
    purse = await db.get(Purse, contribution.purse_id)

    if member_user is None or not member_user.is_verified:
        raise ForbiddenError("email verification required before paying", code="email_not_verified")

    notifications = NotificationService(db, sendbyte)
    contribution = await service.generate_invoice(contribution, monnify, member, member_user, purse, notifications)

    return success_response(
        {
            "account_number": contribution.account_number,
            "bank_name": contribution.bank_name,
            "amount": str(contribution.amount_expected - contribution.amount_received),
            "expires_at": contribution.invoice_expires_at.isoformat(),
        }
    )


@router.post("/{contribution_id}/mark-manual", response_model=StandardResponse[MarkManualResponse])
async def mark_manual(
    contribution_id: UUID,
    payload: MarkManualRequest,
    idempotency_key: str | None = Depends(get_idempotency_key),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    service: ContributionService = Depends(get_contribution_service),
    idem_store: IdempotencyStore = Depends(get_idempotency_store),
) -> JSONResponse:
    if current_user.role != "group_admin":
        raise ForbiddenError("only a group admin can log a manual payment")

    contribution = await service.get_by_id(contribution_id)
    admin, _purse = await _assert_admin_owns(db, current_user, contribution)

    request_fingerprint = fingerprint(payload.model_dump(mode="json"))
    if idempotency_key is not None:
        cached = await idem_store.begin(
            IDEMPOTENCY_SCOPE_MARK_MANUAL, admin.id, idempotency_key, request_fingerprint
        )
        if cached is not None:
            return JSONResponse(status_code=cached["status_code"], content=cached["body"])

    try:
        contribution = await service.mark_manual(contribution, admin, payload.amount_received, payload.note)
    except Exception:
        if idempotency_key is not None:
            await idem_store.release(IDEMPOTENCY_SCOPE_MARK_MANUAL, admin.id, idempotency_key)
        raise

    envelope_body = {"success": True, "data": {"id": str(contribution.id), "status": contribution.status.value}, "error": None}
    if idempotency_key is not None:
        await idem_store.complete(
            IDEMPOTENCY_SCOPE_MARK_MANUAL, admin.id, idempotency_key, request_fingerprint, 200, envelope_body
        )
    return JSONResponse(status_code=200, content=envelope_body)


@router.get("/{contribution_id}/history", response_model=StandardResponse[list[ContributionHistoryItem]])
async def get_history(
    contribution_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    service: ContributionService = Depends(get_contribution_service),
) -> JSONResponse:
    contribution = await service.get_by_id(contribution_id)

    if current_user.role == "group_admin":
        await _assert_admin_owns(db, current_user, contribution)
    else:
        await _assert_member_owns(db, current_user, contribution)

    events = await service.list_history(contribution_id)
    return success_response(
        [
            {
                "from_status": e.from_status.value,
                "to_status": e.to_status.value,
                "actor_type": e.actor_type.value,
                "actor_id": str(e.actor_id) if e.actor_id else None,
                "note": e.note,
                "created_at": e.created_at.isoformat(),
            }
            for e in events
        ]
    )


@router.post("/{contribution_id}/resolve-flag", response_model=StandardResponse[ResolveFlagResponse])
async def resolve_flag(
    contribution_id: UUID,
    payload: ResolveFlagRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    service: ContributionService = Depends(get_contribution_service),
) -> JSONResponse:
    if current_user.role != "group_admin":
        raise ForbiddenError("only a group admin can resolve a flagged contribution")

    contribution = await service.get_by_id(contribution_id)
    admin, _purse = await _assert_admin_owns(db, current_user, contribution)

    contribution = await service.resolve_flag(contribution, admin, payload.resolution)
    return success_response({"id": str(contribution.id), "status": contribution.status.value})
