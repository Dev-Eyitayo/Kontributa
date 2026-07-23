import json

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.db import get_db
from app.core.exceptions import AuthError
from app.core.response import StandardResponse, success_response
from app.modules.notifications.service import SendByteClient, get_sendbyte_client
from app.modules.payments.service import MonnifyClient
from app.modules.webhooks.schemas import ReceivedResponse
from app.modules.webhooks.service import (
    WebhookService,
    process_collection_webhook_event,
    process_transfer_webhook_event,
)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/monnify", status_code=202, response_model=StandardResponse[ReceivedResponse])
async def monnify_webhook(
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
    provider_event_id = event_data.get("transactionReference") or payload.get("transactionReference")

    service = WebhookService(db)
    event, is_new = await service.store_event(provider_event_id, raw_body.decode(), signature_valid=True)

    if is_new:
        session_factory = async_sessionmaker(bind=db.bind, expire_on_commit=False)
        background_tasks.add_task(process_collection_webhook_event, event.id, session_factory, sendbyte)

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
