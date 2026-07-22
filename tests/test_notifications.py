import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select

from app.core.auth import create_access_token
from app.core.config import settings
from app.modules.contributions.models import Contribution
from app.modules.notifications.models import NotificationLog
from app.modules.purses.models import Purse
from tests.conftest import _state, create_org_and_group, create_platform_admin, find_redis_token


async def _admin_platform_headers(db_session):
    admin = await create_platform_admin(db_session)
    token = create_access_token(admin.id, "group_admin")
    return {"Authorization": f"Bearer {token.token}"}


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
    await client.post("/auth/verify-email", json={"token": verify_token})
    login = await client.post("/auth/login", json={"email": email, "password": "password123"})
    return login.json()["data"]["access_token"]


async def _register_and_login_member(client, token, email, first_name="Ada", last_name="Lovelace"):
    await client.post(
        f"/members/join/{token}",
        json={"email": email, "password": "password123", "first_name": first_name, "last_name": last_name},
    )
    verify_token = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"token": verify_token})
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


async def _setup_purse_with_two_members(client, db_session, amount="1000.00"):
    """One member gets marked paid immediately; the other is left pending --
    used to assert reminders go only to the still-pending one."""
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
    invite_token = invite.json()["data"]["token"]
    await _register_and_login_member(client, invite_token, "paid-member@example.com", "Paid", "Member")
    await _register_and_login_member(client, invite_token, "pending-member@example.com", "Pending", "Member")

    create = await client.post(
        "/purses",
        json={"title": "Dues", "amount": amount, "deadline": _future_deadline(), "enroll_mode": "snapshot"},
        headers=admin_headers,
    )
    purse_id = create.json()["data"]["id"]

    from app.modules.auth.models import User
    from app.modules.members.models import Member

    rows = (
        await db_session.execute(
            select(Contribution, Member, User)
            .join(Member, Contribution.member_id == Member.id)
            .join(User, Member.user_id == User.id)
            .where(Contribution.purse_id == purse_id)
        )
    ).all()
    paid_contribution_id = next(str(c.id) for c, m, u in rows if u.email == "paid-member@example.com")

    mark = await client.post(
        f"/contributions/{paid_contribution_id}/mark-manual",
        json={"amount_received": amount, "note": "cash collected"},
        headers=admin_headers,
    )
    assert mark.status_code == 200, mark.text

    return org, group, admin_headers, purse_id, "pending-member@example.com"


def _sign(body: bytes) -> str:
    return hmac.new(settings.MONNIFY_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()


def _collection_webhook_body(payment_reference: str, amount_paid: str) -> bytes:
    payload = {
        "eventType": "SUCCESSFUL_TRANSACTION",
        "eventData": {
            "transactionReference": f"MNFY|{uuid4().hex}",
            "paymentReference": payment_reference,
            "amountPaid": amount_paid,
            "paymentStatus": "PAID",
            # Real Monnify collection webhooks send paidOn with milliseconds.
            "paidOn": "2026-07-22 15:14:00.000",
        },
    }
    return json.dumps(payload).encode()


async def _generate_invoice(client, headers, contribution_id) -> dict:
    resp = await client.post(f"/contributions/{contribution_id}/generate-invoice", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def _setup_purse_with_paid_contribution(client, db_session, collected="2500.00", email="rep@example.com"):
    org, group = await create_org_and_group(db_session)
    admin_token = await _register_and_login_group_admin(client, email=email)
    headers = {"Authorization": f"Bearer {admin_token}"}
    await client.post(
        "/group-admins/onboard",
        json={"organization_id": str(org.id), "group_id": str(group.id)},
        headers=headers,
    )

    invite = await client.post("/group-admins/invite-links", json={"expires_in_days": 7}, headers=headers)
    token = invite.json()["data"]["token"]
    await client.post(
        f"/members/join/{token}",
        json={"email": f"member-{email}", "password": "password123", "first_name": "Member", "last_name": "One"},
    )

    create = await client.post(
        "/purses",
        json={"title": "Fee", "amount": collected, "deadline": _future_deadline(), "enroll_mode": "snapshot"},
        headers=headers,
    )
    purse_id = create.json()["data"]["id"]

    result = await db_session.execute(select(Contribution).where(Contribution.purse_id == purse_id))
    contribution = result.scalar_one()

    mark = await client.post(
        f"/contributions/{contribution.id}/mark-manual",
        json={"amount_received": collected, "note": "cash collected"},
        headers=headers,
    )
    assert mark.status_code == 200, mark.text

    return org, group, headers, purse_id


async def _register_settlement_account(client, group_id, headers, account_number="0123456789"):
    resp = await client.post(
        f"/groups/{group_id}/settlement-account",
        json={
            "bank_code": "058",
            "account_number": account_number,
            "confirmed_account_name": "Default Resolved Name",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


# --- Auth emails ---------------------------------------------------------


async def test_register_sends_verification_email(client):
    resp = await client.post(
        "/auth/register",
        json={
            "email": "newuser@example.com",
            "password": "password123",
            "first_name": "New",
            "last_name": "User",
            "role": "group_admin",
        },
    )
    assert resp.status_code == 201

    sent = _state["sendbyte"].sent
    assert len(sent) == 1
    assert sent[0]["to_email"] == "newuser@example.com"
    assert "Verify your Kontributa account" in sent[0]["subject"]


async def test_forgot_password_sends_reset_email(client, db_session):
    await client.post(
        "/auth/register",
        json={
            "email": "resetme@example.com",
            "password": "password123",
            "first_name": "Reset",
            "last_name": "Me",
            "role": "group_admin",
        },
    )
    _state["sendbyte"].sent.clear()

    resp = await client.post("/auth/forgot-password", json={"email": "resetme@example.com"})
    assert resp.status_code == 200

    sent = _state["sendbyte"].sent
    assert len(sent) == 1
    assert sent[0]["to_email"] == "resetme@example.com"
    assert "Reset your Kontributa password" in sent[0]["subject"]


async def test_register_rate_limited_after_burst(client):
    responses = []
    for i in range(settings.RATE_LIMIT_REGISTER_PER_HOUR + 1):
        responses.append(
            await client.post(
                "/auth/register",
                json={
                    "email": f"burst{i}@example.com",
                    "password": "password123",
                    "first_name": "Burst",
                    "last_name": "Tester",
                    "role": "group_admin",
                },
            )
        )
    assert responses[-1].status_code == 429
    assert responses[-1].json()["error"]["code"] == "rate_limited"


# --- Contribution emails --------------------------------------------------


async def test_receipt_email_sent_on_webhook_paid(client, db_session):
    ctx = await _setup_purse_with_member(client, db_session, amount="2500.00")
    await _generate_invoice(client, ctx["member_headers"], ctx["contribution_id"])
    _state["sendbyte"].sent.clear()  # discard the join-time verification email

    result = await db_session.execute(select(Contribution).where(Contribution.id == ctx["contribution_id"]))
    contribution = result.scalar_one()

    body = _collection_webhook_body(contribution.invoice_id, "2500.00")
    resp = await client.post("/webhooks/monnify", content=body, headers={"monnify-signature": _sign(body)})
    assert resp.status_code == 202

    matching = [e for e in _state["sendbyte"].sent if e["to_email"] == "ada@example.com"]
    assert len(matching) == 1
    assert "Payment received" in matching[0]["subject"]


async def test_expiry_notice_sent_when_invoice_lapses(client, db_session):
    ctx = await _setup_purse_with_member(client, db_session)
    await _generate_invoice(client, ctx["member_headers"], ctx["contribution_id"])
    _state["sendbyte"].sent.clear()  # discard the join-time verification email

    result = await db_session.execute(select(Contribution).where(Contribution.id == ctx["contribution_id"]))
    contribution = result.scalar_one()
    contribution.invoice_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db_session.commit()

    # Triggers expire_if_needed (with notifications wired through
    # generate_invoice) as the first step of generating a fresh invoice.
    await _generate_invoice(client, ctx["member_headers"], ctx["contribution_id"])

    matching = [e for e in _state["sendbyte"].sent if e["to_email"] == "ada@example.com"]
    assert len(matching) == 1
    assert "expired" in matching[0]["subject"]


async def test_email_failure_does_not_block_payment_confirmation(client, db_session):
    ctx = await _setup_purse_with_member(client, db_session, amount="2500.00")
    await _generate_invoice(client, ctx["member_headers"], ctx["contribution_id"])
    _state["sendbyte"].should_fail = True

    result = await db_session.execute(select(Contribution).where(Contribution.id == ctx["contribution_id"]))
    contribution = result.scalar_one()

    body = _collection_webhook_body(contribution.invoice_id, "2500.00")
    resp = await client.post("/webhooks/monnify", content=body, headers={"monnify-signature": _sign(body)})
    assert resp.status_code == 202

    check = await client.get(f"/contributions/{ctx['contribution_id']}", headers=ctx["admin_headers"])
    assert check.json()["data"]["status"] == "paid"

    log_result = await db_session.execute(
        select(NotificationLog).where(
            NotificationLog.to_email == "ada@example.com",
            NotificationLog.template_name == "payment_receipt.html",
        )
    )
    log = log_result.scalar_one()
    assert log.status.value == "failed"


async def test_notification_log_records_each_send(client, db_session):
    resp = await client.post(
        "/auth/register",
        json={
            "email": "logtest@example.com",
            "password": "password123",
            "first_name": "Log",
            "last_name": "Test",
            "role": "group_admin",
        },
    )
    assert resp.status_code == 201

    result = await db_session.execute(select(NotificationLog).where(NotificationLog.to_email == "logtest@example.com"))
    log = result.scalar_one()
    assert log.status.value == "sent"
    assert log.template_name == "verify_email.html"


# --- Payout emails ---------------------------------------------------------


async def test_payout_completed_email_sent_to_rep(client, db_session):
    org, group, headers, purse_id = await _setup_purse_with_paid_contribution(client, db_session, collected="2500.00")
    await _register_settlement_account(client, group.id, headers)
    _state["sendbyte"].sent.clear()

    create = await client.post(
        "/payouts", json={"group_id": str(group.id), "purse_id": purse_id, "amount": "2000.00"}, headers=headers
    )
    payout_id = create.json()["data"]["id"]

    admin_headers = await _admin_platform_headers(db_session)
    await client.post(f"/payouts/{payout_id}/approve", headers=admin_headers)

    detail = await client.get(f"/payouts/{payout_id}", headers=headers)
    transfer_ref = detail.json()["data"]["monnify_transfer_ref"]

    body = json.dumps(
        {
            "eventType": "SUCCESSFUL_DISBURSEMENT",
            "eventData": {"transactionReference": f"MNFY|{transfer_ref}", "reference": transfer_ref},
        }
    ).encode()
    resp = await client.post("/webhooks/monnify/transfers", content=body, headers={"monnify-signature": _sign(body)})
    assert resp.status_code == 202

    matching = [e for e in _state["sendbyte"].sent if e["to_email"] == "rep@example.com"]
    assert len(matching) == 1
    assert "Payout completed" in matching[0]["subject"]


async def test_payout_failed_email_sent_to_rep(client, db_session):
    org, group, headers, purse_id = await _setup_purse_with_paid_contribution(client, db_session, collected="2500.00")
    await _register_settlement_account(client, group.id, headers)
    _state["monnify"].transfer_should_fail = True
    _state["sendbyte"].sent.clear()

    create = await client.post(
        "/payouts", json={"group_id": str(group.id), "purse_id": purse_id, "amount": "2000.00"}, headers=headers
    )
    payout_id = create.json()["data"]["id"]

    admin_headers = await _admin_platform_headers(db_session)
    await client.post(f"/payouts/{payout_id}/approve", headers=admin_headers)

    matching = [e for e in _state["sendbyte"].sent if e["to_email"] == "rep@example.com"]
    assert len(matching) == 1
    assert "Payout failed" in matching[0]["subject"]


# --- Purse reminders --------------------------------------------------------


async def test_remind_sends_only_to_pending_members(client, db_session):
    org, group, admin_headers, purse_id, pending_email = await _setup_purse_with_two_members(client, db_session)
    _state["sendbyte"].sent.clear()

    resp = await client.post(f"/purses/{purse_id}/remind", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "reminders_queued"

    sent = _state["sendbyte"].sent
    assert len(sent) == 1
    assert sent[0]["to_email"] == pending_email


async def test_remind_weekly_cooldown_blocks_immediate_repeat(client, db_session):
    org, group, admin_headers, purse_id, pending_email = await _setup_purse_with_two_members(client, db_session)
    _state["sendbyte"].sent.clear()

    first = await client.post(f"/purses/{purse_id}/remind", headers=admin_headers)
    assert first.status_code == 200

    second = await client.post(f"/purses/{purse_id}/remind", headers=admin_headers)
    assert second.status_code == 422
    assert second.json()["error"]["code"] == "reminder_too_soon"
    assert len(_state["sendbyte"].sent) == 1

    result = await db_session.execute(select(Purse).where(Purse.id == purse_id))
    purse = result.scalar_one()
    purse.last_reminder_sent_at = datetime.now(timezone.utc) - timedelta(
        days=settings.REMINDER_MIN_INTERVAL_DAYS, hours=1
    )
    await db_session.commit()

    third = await client.post(f"/purses/{purse_id}/remind", headers=admin_headers)
    assert third.status_code == 200
    assert len(_state["sendbyte"].sent) == 2


async def test_remind_disabled_via_settings(client, db_session, monkeypatch):
    org, group, admin_headers, purse_id, pending_email = await _setup_purse_with_two_members(client, db_session)
    monkeypatch.setattr(settings, "REMINDERS_ENABLED", False)
    _state["sendbyte"].sent.clear()

    resp = await client.post(f"/purses/{purse_id}/remind", headers=admin_headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "reminders_disabled"
    assert _state["sendbyte"].sent == []


async def test_remind_rate_limited_after_burst(client, db_session):
    org, group, admin_headers, purse_id, pending_email = await _setup_purse_with_two_members(client, db_session)

    responses = []
    for _ in range(settings.RATE_LIMIT_REMIND_PER_MINUTE + 1):
        responses.append(await client.post(f"/purses/{purse_id}/remind", headers=admin_headers))

    assert responses[-1].status_code == 429
    assert responses[-1].json()["error"]["code"] == "rate_limited"


async def test_remind_scoped_to_own_group(client, db_session):
    org, group, admin_headers, purse_id, pending_email = await _setup_purse_with_two_members(client, db_session)

    other_org, other_group = await create_org_and_group(
        db_session, org_name="Other Uni", org_short_code="OU9", group_name="Other Dept", group_short_code="OD9"
    )
    other_token = await _register_and_login_group_admin(client, email="other-rep@example.com")
    other_headers = {"Authorization": f"Bearer {other_token}"}
    await client.post(
        "/group-admins/onboard",
        json={"organization_id": str(other_org.id), "group_id": str(other_group.id)},
        headers=other_headers,
    )

    resp = await client.post(f"/purses/{purse_id}/remind", headers=other_headers)
    assert resp.status_code == 403
