from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query
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
from app.modules.realtime.service import RealtimeService, get_realtime_service

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
    realtime: RealtimeService = Depends(get_realtime_service),
) -> JSONResponse:
    purse_id = payload.purse_id if payload else None
    force = payload.force if (payload and payload.force is not None) else True
    notifications = NotificationService(db, sendbyte)
    checked, updated = await run_reconciliation(db, monnify, purse_id, notifications, realtime, force=force)
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


@router.post("/webhook-events/{event_id}/reprocess", response_model=StandardResponse[dict])
async def reprocess_webhook_event(
    event_id: str,
    background_tasks: BackgroundTasks,
    _: CurrentUser = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
    sendbyte: SendByteClient = Depends(get_sendbyte_client),
    realtime: RealtimeService = Depends(get_realtime_service),
) -> JSONResponse:
    import json
    from uuid import UUID
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.core.exceptions import NotFoundError
    from app.modules.webhooks.models import WebhookEvent
    from app.modules.webhooks.service import (
        process_collection_webhook_event,
        process_settlement_webhook_event,
        process_transfer_webhook_event,
    )

    event = None
    try:
        event = await db.get(WebhookEvent, UUID(event_id))
    except Exception:
        pass

    if event is None:
        result = await db.execute(select(WebhookEvent).where(WebhookEvent.provider_event_id == event_id))
        event = result.scalar_one_or_none()

    if event is None:
        raise NotFoundError("webhook event not found", code="webhook_event_not_found")

    event.processed = False
    event.processing_error = None
    await db.commit()

    session_factory = async_sessionmaker(bind=db.bind, expire_on_commit=False)
    payload = json.loads(event.raw_payload)
    event_type = payload.get("event") or payload.get("eventType")

    if event_type in ("subaccount.settlement", "settlement.success", "SUCCESSFUL_SETTLEMENT", "SETTLEMENT_COMPLETED"):
        background_tasks.add_task(process_settlement_webhook_event, event.id, session_factory, payload)
    elif event_type in ("transfer.success", "transfer.failed", "transfer.reversed", "SUCCESSFUL_DISBURSEMENT", "FAILED_DISBURSEMENT", "REVERSED_DISBURSEMENT"):
        background_tasks.add_task(process_transfer_webhook_event, event.id, session_factory, sendbyte)
    else:
        background_tasks.add_task(process_collection_webhook_event, event.id, session_factory, sendbyte, realtime)

    return success_response({"reprocessed": True, "event_id": str(event.id), "provider_event_id": event.provider_event_id})


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
                    "member_id": str(c.member_id) if c.member_id is not None else None,
                    "group_admin_id": str(c.group_admin_id) if c.group_admin_id is not None else None,
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
