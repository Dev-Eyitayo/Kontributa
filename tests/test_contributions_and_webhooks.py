import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import select

from app.core.config import settings
from app.modules.contributions.models import Contribution
from app.modules.purses.models import Purse
from tests.conftest import _state, create_org_and_group, find_redis_token, onboard_group_admin


async def _register_and_login_group_admin(client, email="rep@example.com"):
    await client.post(
        "/auth/register",
        json={
            "email": email,
            "password": "P@ssword123",
            "first_name": "Tayo",
            "last_name": "Rep",
            "role": "group_admin",
        },
    )
    verify_token = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"email": email, "token": verify_token})
    login = await client.post("/auth/login", json={"email": email, "password": "P@ssword123"})
    return login.json()["data"]["access_token"]


async def _register_and_login_member(client, token, email, first_name="Ada", last_name="Lovelace"):
    await client.post(
        f"/members/join/{token}",
        json={"email": email, "password": "P@ssword123", "first_name": first_name, "last_name": last_name},
    )
    verify_token = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"email": email, "token": verify_token})
    login = await client.post("/auth/login", json={"email": email, "password": "P@ssword123"})
    return login.json()["data"]["access_token"]


def _future_deadline(days=7) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


async def _setup_purse_with_member(client, db_session, amount="2500.00"):
    org, _existing_group = await create_org_and_group(db_session)
    admin_token = await _register_and_login_group_admin(client)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    group = await onboard_group_admin(client, db_session, org, admin_headers)

    invite = await client.post(
        f"/group-admins/invite-links?group_id={group.id}", json={"expires_in_days": 7}, headers=admin_headers
    )
    token = invite.json()["data"]["token"]
    member_token = await _register_and_login_member(client, token, "ada@example.com")
    member_headers = {"Authorization": f"Bearer {member_token}"}

    create = await client.post(
        "/purses",
        json={
            "group_id": str(group.id),
            "title": "Project Defense Fee",
            "amount": amount,
            "deadline": _future_deadline(),
            "enroll_mode": "snapshot",
        },
        headers=admin_headers,
    )
    purse_id = create.json()["data"]["id"]

    # member_id IS NOT NULL -- a purse now also gets one admin-owned row
    # for the group's own admin at creation time (see
    # ContributionService.generate_for_purse), so "the one contribution
    # for this purse" is no longer unambiguous on its own.
    result = await db_session.execute(
        select(Contribution).where(Contribution.purse_id == UUID(purse_id), Contribution.member_id.is_not(None))
    )
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


def _rejected_payment_webhook_body(payment_reference: str, amount: str, expected_amount: str) -> bytes:
    """Monnify's *default* behavior for a dynamic invoice: a transfer that
    doesn't match the invoice's exact amount is rejected and reversed, and
    this fires instead of SUCCESSFUL_TRANSACTION (confirmed against
    Monnify's own webhook event-type docs) -- distinct from the
    underpayment/overpayment SUCCESSFUL_TRANSACTION tests above, which
    only apply once a merchant has explicitly configured their Monnify
    contract to accept mismatched amounts instead of rejecting them."""
    payload = {
        "eventType": "REJECTED_PAYMENT",
        "eventData": {
            "paymentReference": payment_reference,
            "amount": amount,
            "transactionReference": f"MNFY|{uuid4().hex}",
            "paymentRejectionInformation": {
                "rejectionReason": "UNDER_PAYMENT",
                "expectedAmount": expected_amount,
            },
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
    result = await db_session.execute(select(Contribution).where(Contribution.id == UUID(ctx["contribution_id"])))
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


async def test_generate_invoice_rejected_once_purse_deadline_passed(client, db_session):
    ctx = await _setup_purse_with_member(client, db_session)

    # Expire the invoice AND push the purse's own deadline into the past --
    # regeneration must stop once the purse itself is no longer open, not
    # just because the previous invoice expired.
    result = await db_session.execute(select(Contribution).where(Contribution.id == UUID(ctx["contribution_id"])))
    contribution = result.scalar_one()
    contribution.invoice_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db_session.commit()

    purse = await db_session.get(Purse, UUID(ctx["purse_id"]))
    purse.deadline = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db_session.commit()

    resp = await client.post(
        f"/contributions/{ctx['contribution_id']}/generate-invoice", headers=ctx["member_headers"]
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "purse_closed"


async def test_webhook_wrong_signature_rejected(client, db_session):
    ctx = await _setup_purse_with_member(client, db_session)
    await _generate_invoice(client, ctx["member_headers"], ctx["contribution_id"])

    result = await db_session.execute(select(Contribution).where(Contribution.id == UUID(ctx["contribution_id"])))
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

    result = await db_session.execute(select(Contribution).where(Contribution.id == UUID(ctx["contribution_id"])))
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

    result = await db_session.execute(select(Contribution).where(Contribution.id == UUID(ctx["contribution_id"])))
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

    result = await db_session.execute(select(Contribution).where(Contribution.id == UUID(ctx["contribution_id"])))
    contribution = result.scalar_one()

    body = _webhook_body(contribution.invoice_id, "2000.00")
    resp = await client.post("/webhooks/monnify", content=body, headers={"monnify-signature": _sign(body)})
    assert resp.status_code == 202

    detail = await client.get(f"/contributions/{ctx['contribution_id']}", headers=ctx["member_headers"])
    assert detail.json()["data"]["status"] == "flagged_for_review"


async def test_webhook_overpayment_flags_for_review(client, db_session):
    ctx = await _setup_purse_with_member(client, db_session, amount="2500.00")
    await _generate_invoice(client, ctx["member_headers"], ctx["contribution_id"])

    result = await db_session.execute(select(Contribution).where(Contribution.id == UUID(ctx["contribution_id"])))
    contribution = result.scalar_one()

    body = _webhook_body(contribution.invoice_id, "3000.00")
    resp = await client.post("/webhooks/monnify", content=body, headers={"monnify-signature": _sign(body)})
    assert resp.status_code == 202

    detail = await client.get(f"/contributions/{ctx['contribution_id']}", headers=ctx["member_headers"])
    assert detail.json()["data"]["status"] == "flagged_for_review"


async def test_webhook_rejected_payment_leaves_contribution_pending(client, db_session):
    """Monnify's default contract configuration rejects and reverses a
    mismatched-amount transfer rather than completing it -- no money was
    actually received, so this must never flag or otherwise change the
    contribution's status (the member can just regenerate and retry with
    the exact amount). The event should still be recorded, though, not
    silently dropped -- see WebhookEvent.processing_error below."""
    ctx = await _setup_purse_with_member(client, db_session, amount="2500.00")
    await _generate_invoice(client, ctx["member_headers"], ctx["contribution_id"])

    result = await db_session.execute(select(Contribution).where(Contribution.id == UUID(ctx["contribution_id"])))
    contribution = result.scalar_one()

    body = _rejected_payment_webhook_body(contribution.invoice_id, amount="2000.00", expected_amount="2500.00")
    resp = await client.post("/webhooks/monnify", content=body, headers={"monnify-signature": _sign(body)})
    assert resp.status_code == 202

    detail = await client.get(f"/contributions/{ctx['contribution_id']}", headers=ctx["member_headers"])
    assert detail.json()["data"]["status"] == "pending"
    assert detail.json()["data"]["amount_received"] == "0.00"

    from app.modules.webhooks.models import WebhookEvent

    event_result = await db_session.execute(
        select(WebhookEvent).where(WebhookEvent.raw_payload == body.decode())
    )
    event = event_result.scalar_one()
    assert event.processed is True
    assert "rejected" in event.processing_error.lower()
    assert "UNDER_PAYMENT" in event.processing_error


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

    result = await db_session.execute(select(Contribution).where(Contribution.id == UUID(ctx["contribution_id"])))
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

    result = await db_session.execute(select(Contribution).where(Contribution.id == UUID(ctx["contribution_id"])))
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

    result = await db_session.execute(select(Contribution).where(Contribution.id == UUID(ctx["contribution_id"])))
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
        (
            await client.post(
                f"/group-admins/invite-links?group_id={ctx['group'].id}",
                json={"expires_in_days": 7},
                headers=ctx["admin_headers"],
            )
        ).json()["data"]["token"],
        "other@example.com",
        first_name="Other",
        last_name="Member",
    )
    other_headers = {"Authorization": f"Bearer {other_member_token}"}

    resp = await client.get(f"/contributions/{ctx['contribution_id']}", headers=other_headers)
    assert resp.status_code == 403


async def test_purse_summary_splits_collected_by_source(client, db_session):
    """The purse overview needs to distinguish real, Monnify-confirmed
    money (paid via webhook) from a rep's own offline/cash record
    (mark-manual) -- collected_via_kontributa and collected_manually
    should each reflect only their own source, while total_collected
    stays their sum for existing progress displays."""
    org, _existing_group = await create_org_and_group(db_session)
    admin_token = await _register_and_login_group_admin(client)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    group = await onboard_group_admin(client, db_session, org, admin_headers)

    invite_a = await client.post(
        f"/group-admins/invite-links?group_id={group.id}", json={"expires_in_days": 7}, headers=admin_headers
    )
    member_a_token = await _register_and_login_member(
        client, invite_a.json()["data"]["token"], "kontributa-payer@example.com", first_name="Kay", last_name="One"
    )
    member_a_headers = {"Authorization": f"Bearer {member_a_token}"}

    invite_b = await client.post(
        f"/group-admins/invite-links?group_id={group.id}", json={"expires_in_days": 7}, headers=admin_headers
    )
    await _register_and_login_member(
        client, invite_b.json()["data"]["token"], "manual-payer@example.com", first_name="Em", last_name="Two"
    )

    # Both members already exist when the purse is created, so a snapshot
    # purse enrolls both of them.
    create = await client.post(
        "/purses",
        json={
            "group_id": str(group.id),
            "title": "Mixed Sources Fee",
            "amount": "1000.00",
            "deadline": _future_deadline(),
            "enroll_mode": "snapshot",
        },
        headers=admin_headers,
    )
    purse_id = create.json()["data"]["id"]

    from app.modules.auth.models import User
    from app.modules.members.models import Member

    rows = (
        await db_session.execute(
            select(Contribution.id, User.email)
            .join(Member, Contribution.member_id == Member.id)
            .join(User, Member.user_id == User.id)
            .where(Contribution.purse_id == UUID(purse_id))
        )
    ).all()
    contribution_by_email = {email: str(cid) for cid, email in rows}

    kontributa_contribution_id = contribution_by_email["kontributa-payer@example.com"]
    await _generate_invoice(client, member_a_headers, kontributa_contribution_id)
    result = await db_session.execute(select(Contribution).where(Contribution.id == UUID(kontributa_contribution_id)))
    invoice_id = result.scalar_one().invoice_id

    body = _webhook_body(invoice_id, "1000.00")
    webhook_resp = await client.post("/webhooks/monnify", content=body, headers={"monnify-signature": _sign(body)})
    assert webhook_resp.status_code == 202

    manual_resp = await client.post(
        f"/contributions/{contribution_by_email['manual-payer@example.com']}/mark-manual",
        json={"amount_received": "1000.00", "note": "paid cash at meeting"},
        headers=admin_headers,
    )
    assert manual_resp.status_code == 200

    summary = await client.get(f"/purses/{purse_id}/summary", headers=admin_headers)
    assert summary.status_code == 200
    data = summary.json()["data"]
    assert data["collected_via_kontributa"] == "1000.00"
    assert data["collected_manually"] == "1000.00"
    # paid_count is fully-settled, electronic and manual combined.
    assert data["paid_count"] == 2
    assert data["total_collected"] == "2000.00"


async def test_rep_cannot_view_contribution_outside_own_purses(client, db_session):
    ctx = await _setup_purse_with_member(client, db_session)

    other_org, _existing_other_group = await create_org_and_group(
        db_session, org_name="Other Uni", org_short_code="OU4", group_name="Other Dept", group_short_code="OD4"
    )
    other_admin_token = await _register_and_login_group_admin(client, email="other-rep@example.com")
    other_headers = {"Authorization": f"Bearer {other_admin_token}"}
    await onboard_group_admin(client, db_session, other_org, other_headers, group_name="Other Group")

    resp = await client.get(f"/contributions/{ctx['contribution_id']}", headers=other_headers)
    assert resp.status_code == 403
