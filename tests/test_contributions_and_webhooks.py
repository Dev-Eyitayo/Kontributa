import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select

from app.core.config import settings
from app.modules.contributions.models import Contribution
from tests.conftest import _state, create_org_and_group, find_redis_token


async def _register_and_login_group_admin(client, email="rep@example.com"):
    await client.post(
        "/auth/register",
        json={
            "email": email,
            "password": "password123",
            "first_name": "Tayo",
            "last_name": "Rep",
            "role": "group_admin",
        },
    )
    verify_token = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"email": email, "token": verify_token})
    login = await client.post("/auth/login", json={"email": email, "password": "password123"})
    return login.json()["data"]["access_token"]


async def _register_and_login_member(client, token, email, first_name="Ada", last_name="Lovelace"):
    await client.post(
        f"/members/join/{token}",
        json={"email": email, "password": "password123", "first_name": first_name, "last_name": last_name},
    )
    verify_token = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"email": email, "token": verify_token})
    login = await client.post("/auth/login", json={"email": email, "password": "password123"})
    return login.json()["data"]["access_token"]


def _future_deadline(days=7) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


async def _setup_purse_with_member(client, db_session, amount="2500.00"):
    org, group = await create_org_and_group(db_session)
    admin_token = await _register_and_login_group_admin(client)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    await client.post(
        "/group-admins/onboard",
        json={"organization_id": str(org.id), "group_id": str(group.id)},
        headers=admin_headers,
    )

    invite = await client.post(
        "/group-admins/invite-links", json={"expires_in_days": 7}, headers=admin_headers
    )
    token = invite.json()["data"]["token"]
    member_token = await _register_and_login_member(client, token, "ada@example.com")
    member_headers = {"Authorization": f"Bearer {member_token}"}

    create = await client.post(
        "/purses",
        json={"title": "Project Defense Fee", "amount": amount, "deadline": _future_deadline(), "enroll_mode": "snapshot"},
        headers=admin_headers,
    )
    purse_id = create.json()["data"]["id"]

    result = await db_session.execute(select(Contribution).where(Contribution.purse_id == purse_id))
    contribution = result.scalar_one()

    return {
        "org": org,
        "group": group,
        "admin_headers": admin_headers,
        "member_headers": member_headers,
        "purse_id": purse_id,
        "contribution_id": str(contribution.id),
    }


def _sign(body: bytes) -> str:
    return hmac.new(settings.MONNIFY_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()


def _webhook_body(payment_reference: str, amount_paid: str, transaction_reference: str | None = None) -> bytes:
    payload = {
        "eventType": "SUCCESSFUL_TRANSACTION",
        "eventData": {
            "transactionReference": transaction_reference or f"MNFY|{uuid4().hex}",
            "paymentReference": payment_reference,
            "amountPaid": amount_paid,
            "paymentStatus": "PAID",
            # Real Monnify collection webhooks send paidOn with milliseconds
            # (confirmed against Monnify's webhook event-type docs) --
            # distinct from disbursement webhooks' dd/MM/yyyy format.
            "paidOn": "2026-07-22 15:14:00.000",
        },
    }
    return json.dumps(payload).encode()


async def _generate_invoice(client, headers, contribution_id) -> dict:
    resp = await client.post(f"/contributions/{contribution_id}/generate-invoice", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def test_generate_invoice_returns_existing_unexpired_invoice(client, db_session):
    ctx = await _setup_purse_with_member(client, db_session)

    first = await _generate_invoice(client, ctx["member_headers"], ctx["contribution_id"])
    second = await _generate_invoice(client, ctx["member_headers"], ctx["contribution_id"])

    assert first["account_number"] == second["account_number"]
    assert len(_state["monnify"].created_invoices) == 1


async def test_generate_invoice_regenerates_after_expiry(client, db_session):
    ctx = await _setup_purse_with_member(client, db_session)
    first = await _generate_invoice(client, ctx["member_headers"], ctx["contribution_id"])

    # Backdate the invoice so it reads as expired without waiting.
    result = await db_session.execute(select(Contribution).where(Contribution.id == ctx["contribution_id"]))
    contribution = result.scalar_one()
    contribution.invoice_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db_session.commit()

    second = await _generate_invoice(client, ctx["member_headers"], ctx["contribution_id"])
    assert second["account_number"] != first["account_number"]
    assert len(_state["monnify"].created_invoices) == 2

    history = await client.get(f"/contributions/{ctx['contribution_id']}/history", headers=ctx["admin_headers"])
    transitions = [(e["from_status"], e["to_status"]) for e in history.json()["data"]["items"]]
    assert ("pending", "expired") in transitions
    assert ("expired", "pending") in transitions

    limited = await client.get(
        f"/contributions/{ctx['contribution_id']}/history?limit=1&offset=0", headers=ctx["admin_headers"]
    )
    body = limited.json()["data"]
    assert body["total"] == 2
    assert body["limit"] == 1
    assert len(body["items"]) == 1


async def test_webhook_wrong_signature_rejected(client, db_session):
    ctx = await _setup_purse_with_member(client, db_session)
    await _generate_invoice(client, ctx["member_headers"], ctx["contribution_id"])

    result = await db_session.execute(select(Contribution).where(Contribution.id == ctx["contribution_id"]))
    contribution = result.scalar_one()

    body = _webhook_body(contribution.invoice_id, "2500.00")
    resp = await client.post(
        "/webhooks/monnify", content=body, headers={"monnify-signature": "not-the-right-signature"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_signature"

    detail = await client.get(f"/contributions/{ctx['contribution_id']}", headers=ctx["member_headers"])
    assert detail.json()["data"]["status"] == "pending"


async def test_webhook_correct_signature_moves_pending_to_paid(client, db_session):
    ctx = await _setup_purse_with_member(client, db_session)
    await _generate_invoice(client, ctx["member_headers"], ctx["contribution_id"])

    result = await db_session.execute(select(Contribution).where(Contribution.id == ctx["contribution_id"]))
    contribution = result.scalar_one()

    body = _webhook_body(contribution.invoice_id, "2500.00")
    resp = await client.post("/webhooks/monnify", content=body, headers={"monnify-signature": _sign(body)})
    assert resp.status_code == 202
    assert resp.json()["data"]["received"] is True

    detail = await client.get(f"/contributions/{ctx['contribution_id']}", headers=ctx["member_headers"])
    assert detail.json()["data"]["status"] == "paid"
    assert detail.json()["data"]["amount_received"] == "2500.00"
    assert detail.json()["data"]["paid_at"] is not None


async def test_webhook_duplicate_delivery_processed_once(client, db_session):
    ctx = await _setup_purse_with_member(client, db_session)
    await _generate_invoice(client, ctx["member_headers"], ctx["contribution_id"])

    result = await db_session.execute(select(Contribution).where(Contribution.id == ctx["contribution_id"]))
    contribution = result.scalar_one()

    body = _webhook_body(contribution.invoice_id, "2500.00", transaction_reference="MNFY|fixed-ref-123")
    headers = {"monnify-signature": _sign(body)}

    first = await client.post("/webhooks/monnify", content=body, headers=headers)
    second = await client.post("/webhooks/monnify", content=body, headers=headers)
    assert first.status_code == 202
    assert second.status_code == 202

    history = await client.get(f"/contributions/{ctx['contribution_id']}/history", headers=ctx["admin_headers"])
    paid_transitions = [e for e in history.json()["data"]["items"] if e["to_status"] == "paid"]
    assert len(paid_transitions) == 1


async def test_webhook_underpayment_flags_for_review(client, db_session):
    ctx = await _setup_purse_with_member(client, db_session, amount="2500.00")
    await _generate_invoice(client, ctx["member_headers"], ctx["contribution_id"])

    result = await db_session.execute(select(Contribution).where(Contribution.id == ctx["contribution_id"]))
    contribution = result.scalar_one()

    body = _webhook_body(contribution.invoice_id, "2000.00")
    resp = await client.post("/webhooks/monnify", content=body, headers={"monnify-signature": _sign(body)})
    assert resp.status_code == 202

    detail = await client.get(f"/contributions/{ctx['contribution_id']}", headers=ctx["member_headers"])
    assert detail.json()["data"]["status"] == "flagged_for_review"


async def test_webhook_overpayment_flags_for_review(client, db_session):
    ctx = await _setup_purse_with_member(client, db_session, amount="2500.00")
    await _generate_invoice(client, ctx["member_headers"], ctx["contribution_id"])

    result = await db_session.execute(select(Contribution).where(Contribution.id == ctx["contribution_id"]))
    contribution = result.scalar_one()

    body = _webhook_body(contribution.invoice_id, "3000.00")
    resp = await client.post("/webhooks/monnify", content=body, headers={"monnify-signature": _sign(body)})
    assert resp.status_code == 202

    detail = await client.get(f"/contributions/{ctx['contribution_id']}", headers=ctx["member_headers"])
    assert detail.json()["data"]["status"] == "flagged_for_review"


async def test_mark_manual_is_distinct_from_webhook_paid(client, db_session):
    ctx = await _setup_purse_with_member(client, db_session)

    resp = await client.post(
        f"/contributions/{ctx['contribution_id']}/mark-manual",
        json={"amount_received": "2500.00", "note": "paid cash at meeting"},
        headers=ctx["admin_headers"],
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "paid_manual"

    history = await client.get(f"/contributions/{ctx['contribution_id']}/history", headers=ctx["admin_headers"])
    events = history.json()["data"]["items"]
    assert events[-1]["actor_type"] == "rep_manual"
    assert events[-1]["to_status"] == "paid_manual"

    detail = await client.get(f"/contributions/{ctx['contribution_id']}", headers=ctx["member_headers"])
    assert detail.json()["data"]["paid_at"] is not None


async def test_mark_manual_idempotency_key_prevents_double_count(client, db_session):
    ctx = await _setup_purse_with_member(client, db_session)
    headers = {**ctx["admin_headers"], "Idempotency-Key": "manual-key-1"}

    payload = {"amount_received": "2500.00", "note": "cash"}
    first = await client.post(f"/contributions/{ctx['contribution_id']}/mark-manual", json=payload, headers=headers)
    second = await client.post(f"/contributions/{ctx['contribution_id']}/mark-manual", json=payload, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200

    detail = await client.get(f"/contributions/{ctx['contribution_id']}", headers=ctx["member_headers"])
    assert detail.json()["data"]["amount_received"] == "2500.00"


async def test_resolve_flag_accept_partial_and_request_topup(client, db_session):
    ctx = await _setup_purse_with_member(client, db_session, amount="2500.00")
    await _generate_invoice(client, ctx["member_headers"], ctx["contribution_id"])

    result = await db_session.execute(select(Contribution).where(Contribution.id == ctx["contribution_id"]))
    contribution = result.scalar_one()
    body = _webhook_body(contribution.invoice_id, "2000.00")
    await client.post("/webhooks/monnify", content=body, headers={"monnify-signature": _sign(body)})

    resolve = await client.post(
        f"/contributions/{ctx['contribution_id']}/resolve-flag",
        json={"resolution": "accept_partial"},
        headers=ctx["admin_headers"],
    )
    assert resolve.status_code == 200
    assert resolve.json()["data"]["status"] == "paid"


async def test_resolve_flag_request_topup_returns_to_pending(client, db_session):
    ctx = await _setup_purse_with_member(client, db_session, amount="2500.00")
    await _generate_invoice(client, ctx["member_headers"], ctx["contribution_id"])

    result = await db_session.execute(select(Contribution).where(Contribution.id == ctx["contribution_id"]))
    contribution = result.scalar_one()
    body = _webhook_body(contribution.invoice_id, "2000.00")
    await client.post("/webhooks/monnify", content=body, headers={"monnify-signature": _sign(body)})

    resolve = await client.post(
        f"/contributions/{ctx['contribution_id']}/resolve-flag",
        json={"resolution": "request_topup"},
        headers=ctx["admin_headers"],
    )
    assert resolve.status_code == 200
    assert resolve.json()["data"]["status"] == "pending"

    # A fresh invoice for a topup contribution should only ask for the shortfall.
    invoice = await _generate_invoice(client, ctx["member_headers"], ctx["contribution_id"])
    assert invoice["amount"] == "500.00"


async def test_resolve_flag_refund_not_yet_supported(client, db_session):
    ctx = await _setup_purse_with_member(client, db_session, amount="2500.00")
    await _generate_invoice(client, ctx["member_headers"], ctx["contribution_id"])

    result = await db_session.execute(select(Contribution).where(Contribution.id == ctx["contribution_id"]))
    contribution = result.scalar_one()
    body = _webhook_body(contribution.invoice_id, "3000.00")
    await client.post("/webhooks/monnify", content=body, headers={"monnify-signature": _sign(body)})

    resolve = await client.post(
        f"/contributions/{ctx['contribution_id']}/resolve-flag",
        json={"resolution": "refund"},
        headers=ctx["admin_headers"],
    )
    assert resolve.status_code == 422
    assert resolve.json()["error"]["code"] == "refund_not_yet_supported"


async def test_member_cannot_view_another_members_contribution(client, db_session):
    ctx = await _setup_purse_with_member(client, db_session)

    other_member_token = await _register_and_login_member(
        client,
        (await client.post("/group-admins/invite-links", json={"expires_in_days": 7}, headers=ctx["admin_headers"])).json()["data"]["token"],
        "other@example.com",
        first_name="Other",
        last_name="Member",
    )
    other_headers = {"Authorization": f"Bearer {other_member_token}"}

    resp = await client.get(f"/contributions/{ctx['contribution_id']}", headers=other_headers)
    assert resp.status_code == 403


async def test_rep_cannot_view_contribution_outside_own_purses(client, db_session):
    ctx = await _setup_purse_with_member(client, db_session)

    other_org, other_group = await create_org_and_group(
        db_session, org_name="Other Uni", org_short_code="OU4", group_name="Other Dept", group_short_code="OD4"
    )
    other_admin_token = await _register_and_login_group_admin(client, email="other-rep@example.com")
    other_headers = {"Authorization": f"Bearer {other_admin_token}"}
    await client.post(
        "/group-admins/onboard",
        json={"organization_id": str(other_org.id), "group_id": str(other_group.id)},
        headers=other_headers,
    )

    resp = await client.get(f"/contributions/{ctx['contribution_id']}", headers=other_headers)
    assert resp.status_code == 403
