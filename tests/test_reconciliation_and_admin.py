from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.core.auth import create_access_token
from app.modules.contributions.models import Contribution
from app.modules.payments.schemas import MonnifyTransactionStatus
from tests.conftest import _state, create_platform_admin
from tests.test_contributions_and_webhooks import _generate_invoice, _setup_purse_with_member


async def _admin_headers(db_session):
    admin = await create_platform_admin(db_session)
    token = create_access_token(admin.id, "group_admin")
    return {"Authorization": f"Bearer {token.token}"}


async def _backdate_past_threshold(db_session, contribution_id: str) -> None:
    result = await db_session.execute(select(Contribution).where(Contribution.id == contribution_id))
    contribution = result.scalar_one()
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    # updated_at has an onupdate=func.now() trigger on ORM flush, so set it
    # via a raw UPDATE to make sure our backdated value actually sticks.
    from sqlalchemy import update

    await db_session.execute(
        update(Contribution).where(Contribution.id == contribution_id).values(updated_at=past)
    )
    await db_session.commit()


async def test_reconciliation_recovers_dropped_webhook(client, db_session):
    ctx = await _setup_purse_with_member(client, db_session, amount="2500.00")
    await _generate_invoice(client, ctx["member_headers"], ctx["contribution_id"])
    await _backdate_past_threshold(db_session, ctx["contribution_id"])

    result = await db_session.execute(select(Contribution).where(Contribution.id == ctx["contribution_id"]))
    contribution = result.scalar_one()
    _state["monnify"].transaction_statuses[contribution.invoice_id] = MonnifyTransactionStatus(
        transaction_reference="MNFY|manual-check",
        payment_reference=contribution.invoice_id,
        payment_status="PAID",
        amount_paid=Decimal("2500.00"),
        paid_on=None,
    )

    admin_headers = await _admin_headers(db_session)
    run = await client.post("/admin/reconciliation/run", headers=admin_headers)
    assert run.status_code == 200
    body = run.json()["data"]
    assert body["checked"] == 1
    assert body["updated"] == 1

    detail = await client.get(f"/contributions/{ctx['contribution_id']}", headers=ctx["member_headers"])
    assert detail.json()["data"]["status"] == "paid"

    history = await client.get(f"/contributions/{ctx['contribution_id']}/history", headers=ctx["admin_headers"])
    events = history.json()["data"]
    assert events[-1]["actor_type"] == "reconciliation_job"
    assert events[-1]["to_status"] == "paid"


async def test_reconciliation_run_twice_does_not_double_apply(client, db_session):
    ctx = await _setup_purse_with_member(client, db_session, amount="2500.00")
    await _generate_invoice(client, ctx["member_headers"], ctx["contribution_id"])
    await _backdate_past_threshold(db_session, ctx["contribution_id"])

    result = await db_session.execute(select(Contribution).where(Contribution.id == ctx["contribution_id"]))
    contribution = result.scalar_one()
    _state["monnify"].transaction_statuses[contribution.invoice_id] = MonnifyTransactionStatus(
        transaction_reference="MNFY|manual-check",
        payment_reference=contribution.invoice_id,
        payment_status="PAID",
        amount_paid=Decimal("2500.00"),
        paid_on=None,
    )

    admin_headers = await _admin_headers(db_session)
    first = await client.post("/admin/reconciliation/run", headers=admin_headers)
    second = await client.post("/admin/reconciliation/run", headers=admin_headers)

    assert first.json()["data"] == {"checked": 1, "updated": 1}
    assert second.json()["data"] == {"checked": 0, "updated": 0}

    history = await client.get(f"/contributions/{ctx['contribution_id']}/history", headers=ctx["admin_headers"])
    paid_transitions = [e for e in history.json()["data"] if e["to_status"] == "paid"]
    assert len(paid_transitions) == 1


async def test_reconciliation_skips_contributions_not_past_threshold(client, db_session):
    ctx = await _setup_purse_with_member(client, db_session, amount="2500.00")
    await _generate_invoice(client, ctx["member_headers"], ctx["contribution_id"])
    # Deliberately not backdated -- still fresh, should not be picked up yet.

    result = await db_session.execute(select(Contribution).where(Contribution.id == ctx["contribution_id"]))
    contribution = result.scalar_one()
    _state["monnify"].transaction_statuses[contribution.invoice_id] = MonnifyTransactionStatus(
        transaction_reference="MNFY|manual-check",
        payment_reference=contribution.invoice_id,
        payment_status="PAID",
        amount_paid=Decimal("2500.00"),
        paid_on=None,
    )

    admin_headers = await _admin_headers(db_session)
    run = await client.post("/admin/reconciliation/run", headers=admin_headers)
    assert run.json()["data"] == {"checked": 0, "updated": 0}

    detail = await client.get(f"/contributions/{ctx['contribution_id']}", headers=ctx["member_headers"])
    assert detail.json()["data"]["status"] == "pending"


async def test_reconciliation_requires_admin(client, db_session):
    ctx = await _setup_purse_with_member(client, db_session)
    resp = await client.post("/admin/reconciliation/run", headers=ctx["admin_headers"])
    assert resp.status_code == 403


async def test_admin_webhook_events_and_flagged_contributions(client, db_session):
    ctx = await _setup_purse_with_member(client, db_session, amount="2500.00")
    await _generate_invoice(client, ctx["member_headers"], ctx["contribution_id"])

    result = await db_session.execute(select(Contribution).where(Contribution.id == ctx["contribution_id"]))
    contribution = result.scalar_one()

    import hashlib
    import hmac
    import json

    from app.core.config import settings

    body = json.dumps(
        {
            "eventType": "SUCCESSFUL_TRANSACTION",
            "eventData": {
                "transactionReference": "MNFY|webhookevt1",
                "paymentReference": contribution.invoice_id,
                "amountPaid": "2000.00",
                "paymentStatus": "PAID",
                # Real Monnify collection webhooks send paidOn with milliseconds.
                "paidOn": "2026-07-22 15:14:00.000",
            },
        }
    ).encode()
    sig = hmac.new(settings.MONNIFY_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()
    await client.post("/webhooks/monnify", content=body, headers={"monnify-signature": sig})

    admin_headers = await _admin_headers(db_session)

    events = await client.get("/admin/webhook-events", headers=admin_headers)
    assert events.status_code == 200
    assert len(events.json()["data"]) == 1
    assert events.json()["data"][0]["processed"] is True
    assert events.json()["data"][0]["signature_valid"] is True

    flagged = await client.get("/admin/contributions/flagged", headers=admin_headers)
    assert flagged.status_code == 200
    assert len(flagged.json()["data"]) == 1
    assert flagged.json()["data"][0]["id"] == ctx["contribution_id"]
    assert flagged.json()["data"][0]["amount_received"] == "2000.00"


async def test_admin_endpoints_require_admin_role(client, db_session):
    ctx = await _setup_purse_with_member(client, db_session)
    events = await client.get("/admin/webhook-events", headers=ctx["admin_headers"])
    assert events.status_code == 403
    flagged = await client.get("/admin/contributions/flagged", headers=ctx["admin_headers"])
    assert flagged.status_code == 403
