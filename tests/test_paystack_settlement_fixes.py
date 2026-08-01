import hmac
import hashlib
import json
import pytest
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.modules.platform_settings.models import PlatformSettings
from app.modules.group_admins.models import GroupAdmin
from app.modules.webhooks.models import WebhookEvent
from app.modules.webhooks.service import process_settlement_webhook_event
from app.modules.settlement.models import MonnifySettlementLog, SettlementAccount, SettlementMode
from tests.conftest import create_org_and_group, create_settlement_account, create_platform_admin


@pytest.mark.asyncio
async def test_process_settlement_webhook_event_resolves_group_id_without_name_error(db_session):
    # 1. Setup group, create group admin, and settlement account with unique short code
    unique_suffix = uuid4().hex[:6]
    org, group = await create_org_and_group(
        db_session,
        org_name=f"Org {unique_suffix}",
        org_short_code=f"O{unique_suffix.upper()}",
        group_name=f"Group {unique_suffix}",
        group_short_code=f"G{unique_suffix.upper()}",
    )
    admin_user = await create_platform_admin(db_session, email=f"admin-{unique_suffix}@example.com")
    group_admin = GroupAdmin(group_id=group.id, user_id=admin_user.id)
    db_session.add(group_admin)
    await db_session.commit()

    settlement_acc = await create_settlement_account(db_session, group, mode=SettlementMode.DIRECT)
    sub_code = settlement_acc.direct_sub_account_code

    # 2. Create a WebhookEvent row
    event_id = uuid4()
    provider_ref = f"monnify-ref-{uuid4().hex[:8]}"
    raw_payload = json.dumps({
        "eventType": "SUCCESSFUL_SETTLEMENT",
        "eventData": {
            "settlementReference": provider_ref,
            "subAccountCode": sub_code,
            "amount": 5000.0,
            "fee": 100.0,
            "settledAmount": 4900.0,
            "status": "COMPLETED",
            "settlementTime": "2026-08-01 12:00:00.000",
            "destinationAccountName": "Test Bank Acc",
            "destinationAccountNumber": "0123456789",
            "destinationBankCode": "058",
            "destinationBankName": "GTBank",
        }
    })
    db_event = WebhookEvent(
        id=event_id,
        provider_event_id=provider_ref,
        raw_payload=raw_payload,
        signature_valid=True,
    )
    db_session.add(db_event)
    await db_session.commit()

    # 3. Process settlement webhook event
    session_factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    payload = json.loads(raw_payload)
    
    await process_settlement_webhook_event(event_id, session_factory, payload)

    # 4. Verify MonnifySettlementLog row created with correct group_id
    log_entry = (
        await db_session.execute(
            select(MonnifySettlementLog).where(MonnifySettlementLog.settlement_reference == provider_ref)
        )
    ).scalar_one_or_none()

    assert log_entry is not None
    assert log_entry.group_id == group.id
    assert log_entry.sub_account_code == sub_code
    assert log_entry.settled_amount == 4900.0


@pytest.mark.asyncio
async def test_paystack_webhook_charge_success_dispatches_correctly(client):
    payload_dict = {
        "event": "charge.success",
        "data": {
            "id": 999999,
            "reference": f"test_paystack_ref_{uuid4().hex[:6]}",
            "amount": 10000,
            "status": "success",
        }
    }
    body_bytes = json.dumps(payload_dict).encode("utf-8")
    secret = settings.PAYSTACK_SECRET_KEY or "test_secret"
    sig = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha512).hexdigest()

    resp = await client.post(
        "/webhooks/paystack",
        content=body_bytes,
        headers={"x-paystack-signature": sig, "Content-Type": "application/json"},
    )
    assert resp.status_code == 202
    assert resp.json()["data"]["received"] is True
