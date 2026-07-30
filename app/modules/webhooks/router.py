import json
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.db import get_db
from app.core.exceptions import AuthError
from app.core.response import StandardResponse, success_response
from app.modules.notifications.service import SendByteClient, get_sendbyte_client
from app.modules.payments.service import MonnifyClient
from app.modules.realtime.service import RealtimeService, get_realtime_service
from app.modules.webhooks.schemas import ReceivedResponse
from app.modules.webhooks.service import (
    WebhookService,
    process_collection_webhook_event,
    process_settlement_webhook_event,
    process_transfer_webhook_event,
)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/monnify", status_code=202, response_model=StandardResponse[ReceivedResponse])
async def monnify_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    sendbyte: SendByteClient = Depends(get_sendbyte_client),
    realtime: RealtimeService = Depends(get_realtime_service),
) -> JSONResponse:
    raw_body = await request.body()
    signature = request.headers.get("monnify-signature", "")

    if not MonnifyClient.verify_signature(raw_body, signature, settings.MONNIFY_SECRET_KEY):
        raise AuthError("invalid webhook signature", code="invalid_signature")

    payload = json.loads(raw_body)
    event_type = payload.get("eventType")
    event_data = payload.get("eventData", {})
    provider_event_id = (
        event_data.get("settlementReference")
        or event_data.get("transactionReference")
        or payload.get("transactionReference")
        or payload.get("settlementReference")
    )

    service = WebhookService(db)
    event, is_new = await service.store_event(provider_event_id, raw_body.decode(), signature_valid=True)

    if is_new:
        session_factory = async_sessionmaker(bind=db.bind, expire_on_commit=False)
        if event_type in ("SUCCESSFUL_SETTLEMENT", "SETTLEMENT_COMPLETED"):
            background_tasks.add_task(process_settlement_webhook_event, event.id, session_factory, payload)
        else:
            background_tasks.add_task(process_collection_webhook_event, event.id, session_factory, sendbyte, realtime)

    return success_response({"received": True}, status_code=202)


@router.post("/monnify/transfers", status_code=202, response_model=StandardResponse[ReceivedResponse])
async def monnify_transfer_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    sendbyte: SendByteClient = Depends(get_sendbyte_client),
) -> JSONResponse:
    raw_body = await request.body()
    signature = request.headers.get("monnify-signature", "")

    if not MonnifyClient.verify_signature(raw_body, signature, settings.MONNIFY_SECRET_KEY):
        raise AuthError("invalid webhook signature", code="invalid_signature")

    payload = json.loads(raw_body)
    event_data = payload.get("eventData", {})
    provider_event_id = (
        event_data.get("transactionReference") or event_data.get("reference") or payload.get("transactionReference")
    )

    service = WebhookService(db)
    event, is_new = await service.store_event(provider_event_id, raw_body.decode(), signature_valid=True)

    if is_new:
        session_factory = async_sessionmaker(bind=db.bind, expire_on_commit=False)
        background_tasks.add_task(process_transfer_webhook_event, event.id, session_factory, sendbyte)

    return success_response({"received": True}, status_code=202)


@router.post("/paystack", status_code=202, response_model=StandardResponse[ReceivedResponse])
async def paystack_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    sendbyte: SendByteClient = Depends(get_sendbyte_client),
    realtime: RealtimeService = Depends(get_realtime_service),
) -> JSONResponse:
    from app.modules.payments.paystack import PaystackClient

    raw_body = await request.body()
    signature = request.headers.get("x-paystack-signature", "")

    paystack = PaystackClient()
    if not paystack.verify_webhook_signature(raw_body, signature):
        raise AuthError("invalid webhook signature", code="invalid_signature")

    payload = json.loads(raw_body)
    event_type = payload.get("event")
    event_data = payload.get("data", {})
    provider_event_id = event_data.get("reference") or event_data.get("id")

    service = WebhookService(db)
    event, is_new = await service.store_event(str(provider_event_id), raw_body.decode(), signature_valid=True)

    if is_new:
        session_factory = async_sessionmaker(bind=db.bind, expire_on_commit=False)
        if event_type in ("subaccount.settlement", "settlement.success"):
            subaccount = event_data.get("subaccount", {})
            sub_code = subaccount.get("subaccount_code") if isinstance(subaccount, dict) else str(subaccount or "")
            settlement_payload = {
                "eventType": "SUCCESSFUL_SETTLEMENT",
                "eventData": {
                    "settlementReference": str(event_data.get("id") or provider_event_id),
                    "subAccountCode": sub_code,
                    "amount": float(Decimal(str(event_data.get("total_amount") or event_data.get("amount", 0))) / Decimal("100")),
                    "fee": float(Decimal(str(event_data.get("total_fees") or event_data.get("fee", 0))) / Decimal("100")),
                    "settledAmount": float(Decimal(str(event_data.get("settled_amount") or event_data.get("amount", 0))) / Decimal("100")),
                    "status": "COMPLETED",
                    "settlementTime": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f"),
                },
            }
            background_tasks.add_task(process_settlement_webhook_event, event.id, session_factory, settlement_payload)
        elif event_type == "charge.success":
            background_tasks.add_task(process_collection_webhook_event, event.id, session_factory, sendbyte, realtime)
        elif event_type in ("transfer.success", "transfer.failed", "transfer.reversed"):
            background_tasks.add_task(process_transfer_webhook_event, event.id, session_factory, sendbyte)

    return success_response({"received": True}, status_code=202)
