from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_admin_user
from app.core.db import get_db
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, Paginated
from app.core.response import StandardResponse, success_response
from app.modules.admin.schemas import (
    FlaggedContributionItem,
    ReconciliationRunRequest,
    ReconciliationRunResponse,
    WebhookEventListItem,
)
from app.modules.admin.service import AdminService
from app.modules.jobs.service import run_reconciliation
from app.modules.notifications.service import NotificationService, SendByteClient, get_sendbyte_client
from app.modules.payments.service import MonnifyClient, get_monnify_client

router = APIRouter(prefix="/admin", tags=["admin"])


def get_admin_service(db: AsyncSession = Depends(get_db)) -> AdminService:
    return AdminService(db)


@router.post("/reconciliation/run", response_model=StandardResponse[ReconciliationRunResponse])
async def trigger_reconciliation(
    payload: Optional[ReconciliationRunRequest] = None,
    _: CurrentUser = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
    monnify: MonnifyClient = Depends(get_monnify_client),
    sendbyte: SendByteClient = Depends(get_sendbyte_client),
) -> JSONResponse:
    purse_id = payload.purse_id if payload else None
    notifications = NotificationService(db, sendbyte)
    checked, updated = await run_reconciliation(db, monnify, purse_id, notifications)
    return success_response({"checked": checked, "updated": updated})


@router.get("/webhook-events", response_model=StandardResponse[Paginated[WebhookEventListItem]])
async def list_webhook_events(
    processed: Optional[bool] = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    _: CurrentUser = Depends(get_current_admin_user),
    service: AdminService = Depends(get_admin_service),
) -> JSONResponse:
    events, total = await service.list_webhook_events(processed, limit, offset)
    return success_response(
        {
            "items": [
                {
                    "id": str(e.id),
                    "provider_event_id": e.provider_event_id,
                    "signature_valid": e.signature_valid,
                    "processed": e.processed,
                    "received_at": e.received_at.isoformat(),
                }
                for e in events
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


@router.get("/contributions/flagged", response_model=StandardResponse[Paginated[FlaggedContributionItem]])
async def list_flagged_contributions(
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    _: CurrentUser = Depends(get_current_admin_user),
    service: AdminService = Depends(get_admin_service),
) -> JSONResponse:
    contributions, total = await service.list_flagged_contributions(limit, offset)
    return success_response(
        {
            "items": [
                {
                    "id": str(c.id),
                    "purse_id": str(c.purse_id),
                    "member_id": str(c.member_id),
                    "amount_expected": str(c.amount_expected),
                    "amount_received": str(c.amount_received),
                }
                for c in contributions
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )
