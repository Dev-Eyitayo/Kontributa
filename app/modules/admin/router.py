from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_admin_user
from app.core.db import get_db
from app.core.response import success_response
from app.modules.admin.schemas import ReconciliationRunRequest
from app.modules.admin.service import AdminService
from app.modules.jobs.service import run_reconciliation
from app.modules.payments.service import MonnifyClient, get_monnify_client

router = APIRouter(prefix="/admin", tags=["admin"])


def get_admin_service(db: AsyncSession = Depends(get_db)) -> AdminService:
    return AdminService(db)


@router.post("/reconciliation/run")
async def trigger_reconciliation(
    payload: Optional[ReconciliationRunRequest] = None,
    _: CurrentUser = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
    monnify: MonnifyClient = Depends(get_monnify_client),
) -> JSONResponse:
    purse_id = payload.purse_id if payload else None
    checked, updated = await run_reconciliation(db, monnify, purse_id)
    return success_response({"checked": checked, "updated": updated})


@router.get("/webhook-events")
async def list_webhook_events(
    processed: Optional[bool] = Query(default=None),
    _: CurrentUser = Depends(get_current_admin_user),
    service: AdminService = Depends(get_admin_service),
) -> JSONResponse:
    events = await service.list_webhook_events(processed)
    return success_response(
        [
            {
                "id": str(e.id),
                "provider_event_id": e.provider_event_id,
                "signature_valid": e.signature_valid,
                "processed": e.processed,
                "received_at": e.received_at.isoformat(),
            }
            for e in events
        ]
    )


@router.get("/contributions/flagged")
async def list_flagged_contributions(
    _: CurrentUser = Depends(get_current_admin_user),
    service: AdminService = Depends(get_admin_service),
) -> JSONResponse:
    contributions = await service.list_flagged_contributions()
    return success_response(
        [
            {
                "id": str(c.id),
                "purse_id": str(c.purse_id),
                "member_id": str(c.member_id),
                "amount_expected": str(c.amount_expected),
                "amount_received": str(c.amount_received),
            }
            for c in contributions
        ]
    )
