import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.core.auth import create_access_token
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
    await client.post("/auth/verify-email", json={"email": email, "token": verify_token})
    login = await client.post("/auth/login", json={"email": email, "password": "password123"})
    return login.json()["data"]["access_token"]


def _future_deadline(days=7) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


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

    from sqlalchemy import select

    from app.modules.contributions.models import Contribution

    result = await db_session.execute(select(Contribution).where(Contribution.purse_id == purse_id))
    contribution = result.scalar_one()

    mark = await client.post(
        f"/contributions/{contribution.id}/mark-manual",
        json={"amount_received": collected, "note": "cash collected"},
        headers=headers,
    )
    assert mark.status_code == 200, mark.text

    return org, group, headers, purse_id


async def _add_purse_with_collected_amount(client, db_session, headers, collected, suffix):
    """Adds another purse (snapshotting the group's existing member(s), same
    as _setup_purse_with_paid_contribution's initial purse) and fully pays
    it. Deliberately does not invite a new member -- snapshot mode captures
    every member already in the group at creation time, so inviting another
    member here would create a second Contribution row on this same purse
    for the group's original member too."""
    create = await client.post(
        "/purses",
        json={"title": f"Fee {suffix}", "amount": collected, "deadline": _future_deadline(), "enroll_mode": "snapshot"},
        headers=headers,
    )
    purse_id = create.json()["data"]["id"]

    from sqlalchemy import select

    from app.modules.contributions.models import Contribution

    result = await db_session.execute(select(Contribution).where(Contribution.purse_id == purse_id))
    contribution = result.scalar_one()

    mark = await client.post(
        f"/contributions/{contribution.id}/mark-manual",
        json={"amount_received": collected, "note": "cash collected"},
        headers=headers,
    )
    assert mark.status_code == 200, mark.text

    return purse_id


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


async def test_settlement_lookup_does_not_save(client, db_session):
    org, group, headers, purse_id = await _setup_purse_with_paid_contribution(client, db_session)

    lookup = await client.post(
        f"/groups/{group.id}/settlement-account/lookup",
        json={"bank_code": "058", "account_number": "0123456789"},
        headers=headers,
    )
    assert lookup.status_code == 200
    assert lookup.json()["data"]["account_name"] == "Default Resolved Name"

    get_resp = await client.get(f"/groups/{group.id}/settlement-account", headers=headers)
    assert get_resp.status_code == 404


async def test_list_banks_returns_monnify_bank_list(client, db_session):
    org, group, headers, purse_id = await _setup_purse_with_paid_contribution(client, db_session)

    resp = await client.get("/banks", headers=headers)
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert len(body) >= 1
    assert {"bank_code", "bank_name"} <= set(body[0].keys())


async def test_list_banks_is_cached_between_calls(client, db_session):
    org, group, headers, purse_id = await _setup_purse_with_paid_contribution(client, db_session)

    first = await client.get("/banks", headers=headers)
    assert first.status_code == 200

    cached_raw = await _state["redis"].get("banks:monnify")
    assert cached_raw is not None

    second = await client.get("/banks", headers=headers)
    assert second.status_code == 200
    assert second.json()["data"] == first.json()["data"]


async def test_list_banks_requires_group_admin_role(client, db_session):
    org, group, headers, purse_id = await _setup_purse_with_paid_contribution(client, db_session)

    invite = await client.post("/group-admins/invite-links", json={"expires_in_days": 7}, headers=headers)
    token = invite.json()["data"]["token"]
    join = await client.post(
        f"/members/join/{token}",
        json={
            "email": "banks-member@example.com",
            "password": "password123",
            "first_name": "Bank",
            "last_name": "Member",
        },
    )
    login = await client.post(
        "/auth/login", json={"email": "banks-member@example.com", "password": "password123"}
    )
    member_headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}

    resp = await client.get("/banks", headers=member_headers)
    assert resp.status_code == 403


async def test_settlement_save_success_and_masked_get(client, db_session):
    org, group, headers, purse_id = await _setup_purse_with_paid_contribution(client, db_session)
    await _register_settlement_account(client, group.id, headers)

    get_resp = await client.get(f"/groups/{group.id}/settlement-account", headers=headers)
    assert get_resp.status_code == 200
    body = get_resp.json()["data"]
    assert body["account_name_verified"] is True
    assert body["account_number"] == "******6789"


async def test_settlement_save_rejects_name_mismatch(client, db_session):
    org, group, headers, purse_id = await _setup_purse_with_paid_contribution(client, db_session)

    resp = await client.post(
        f"/groups/{group.id}/settlement-account",
        json={
            "bank_code": "058",
            "account_number": "0123456789",
            "confirmed_account_name": "A Totally Different Name",
        },
        headers=headers,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "account_name_mismatch"

    get_resp = await client.get(f"/groups/{group.id}/settlement-account", headers=headers)
    assert get_resp.status_code == 404


async def test_settlement_scoped_to_own_group(client, db_session):
    org, group, headers, purse_id = await _setup_purse_with_paid_contribution(client, db_session)

    other_org, other_group = await create_org_and_group(
        db_session, org_name="Other Uni", org_short_code="OU5", group_name="Other Dept", group_short_code="OD5"
    )
    other_admin_token = await _register_and_login_group_admin(client, email="other-rep@example.com")
    other_headers = {"Authorization": f"Bearer {other_admin_token}"}
    await client.post(
        "/group-admins/onboard",
        json={"organization_id": str(other_org.id), "group_id": str(other_group.id)},
        headers=other_headers,
    )

    resp = await client.get(f"/groups/{group.id}/settlement-account", headers=other_headers)
    assert resp.status_code == 403


async def test_available_balance_reflects_collected_amount(client, db_session):
    org, group, headers, purse_id = await _setup_purse_with_paid_contribution(client, db_session, collected="2500.00")

    balance = await client.get(f"/purses/{purse_id}/available-balance", headers=headers)
    assert balance.status_code == 200
    data = balance.json()["data"]
    assert data["collected_total"] == "2500.00"
    assert data["available_balance"] == "2500.00"


async def test_payout_request_rejected_when_exceeds_balance(client, db_session):
    org, group, headers, purse_id = await _setup_purse_with_paid_contribution(client, db_session, collected="2500.00")

    resp = await client.post(
        "/payouts",
        json={"group_id": str(group.id), "purse_id": purse_id, "amount": "3000.00"},
        headers=headers,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "insufficient_balance"


async def test_payout_request_within_balance_succeeds(client, db_session):
    org, group, headers, purse_id = await _setup_purse_with_paid_contribution(client, db_session, collected="2500.00")

    resp = await client.post(
        "/payouts",
        json={"group_id": str(group.id), "purse_id": purse_id, "amount": "2000.00"},
        headers=headers,
    )
    assert resp.status_code == 201
    assert resp.json()["data"]["status"] == "requested"

    balance = await client.get(f"/purses/{purse_id}/available-balance", headers=headers)
    assert balance.json()["data"]["available_balance"] == "500.00"


async def test_two_simultaneous_payout_requests_constrained_by_balance(client, db_session):
    org, group, headers, purse_id = await _setup_purse_with_paid_contribution(client, db_session, collected="2500.00")

    payload = {"group_id": str(group.id), "purse_id": purse_id, "amount": "1500.00"}
    results = await asyncio.gather(
        client.post("/payouts", json=payload, headers=headers),
        client.post("/payouts", json=payload, headers=headers),
    )

    statuses = sorted(r.status_code for r in results)
    assert statuses == [201, 422]

    rejected = next(r for r in results if r.status_code == 422)
    assert rejected.json()["error"]["code"] == "insufficient_balance"

    balance = await client.get(f"/purses/{purse_id}/available-balance", headers=headers)
    assert balance.json()["data"]["available_balance"] == "1000.00"


async def test_payout_approve_initiates_transfer_and_double_approve_conflicts(client, db_session):
    org, group, headers, purse_id = await _setup_purse_with_paid_contribution(client, db_session, collected="2500.00")
    await _register_settlement_account(client, group.id, headers)

    create = await client.post(
        "/payouts", json={"group_id": str(group.id), "purse_id": purse_id, "amount": "2000.00"}, headers=headers
    )
    payout_id = create.json()["data"]["id"]

    admin_headers = await _admin_platform_headers(db_session)
    approve = await client.post(f"/payouts/{payout_id}/approve", headers=admin_headers)
    assert approve.status_code == 200
    assert approve.json()["data"]["status"] == "approved"

    detail = await client.get(f"/payouts/{payout_id}", headers=headers)
    assert detail.json()["data"]["status"] == "processing"
    assert detail.json()["data"]["monnify_transfer_ref"] is not None
    assert len(_state["monnify"].transfers) == 1

    second_approve = await client.post(f"/payouts/{payout_id}/approve", headers=admin_headers)
    assert second_approve.status_code == 409


async def test_payout_reject_has_no_balance_impact(client, db_session):
    org, group, headers, purse_id = await _setup_purse_with_paid_contribution(client, db_session, collected="2500.00")

    create = await client.post(
        "/payouts", json={"group_id": str(group.id), "purse_id": purse_id, "amount": "2000.00"}, headers=headers
    )
    payout_id = create.json()["data"]["id"]

    admin_headers = await _admin_platform_headers(db_session)
    reject = await client.post(
        f"/payouts/{payout_id}/reject", json={"reason": "duplicate request"}, headers=admin_headers
    )
    assert reject.status_code == 200
    assert reject.json()["data"]["status"] == "rejected"

    balance = await client.get(f"/purses/{purse_id}/available-balance", headers=headers)
    assert balance.json()["data"]["available_balance"] == "2500.00"


async def test_transfer_webhook_success_completes_payout(client, db_session):
    org, group, headers, purse_id = await _setup_purse_with_paid_contribution(client, db_session, collected="2500.00")
    await _register_settlement_account(client, group.id, headers)

    create = await client.post(
        "/payouts", json={"group_id": str(group.id), "purse_id": purse_id, "amount": "2000.00"}, headers=headers
    )
    payout_id = create.json()["data"]["id"]

    admin_headers = await _admin_platform_headers(db_session)
    await client.post(f"/payouts/{payout_id}/approve", headers=admin_headers)

    detail = await client.get(f"/payouts/{payout_id}", headers=headers)
    transfer_ref = detail.json()["data"]["monnify_transfer_ref"]

    import hashlib
    import hmac
    import json

    from app.core.config import settings

    body = json.dumps(
        {
            "eventType": "SUCCESSFUL_DISBURSEMENT",
            "eventData": {"transactionReference": f"MNFY|{transfer_ref}", "reference": transfer_ref},
        }
    ).encode()
    sig = hmac.new(settings.MONNIFY_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()

    resp = await client.post("/webhooks/monnify/transfers", content=body, headers={"monnify-signature": sig})
    assert resp.status_code == 202

    final = await client.get(f"/payouts/{payout_id}", headers=headers)
    assert final.json()["data"]["status"] == "completed"


async def test_transfer_webhook_failure_leaves_balance_unchanged_and_retriable(client, db_session):
    org, group, headers, purse_id = await _setup_purse_with_paid_contribution(client, db_session, collected="2500.00")
    await _register_settlement_account(client, group.id, headers)

    create = await client.post(
        "/payouts", json={"group_id": str(group.id), "purse_id": purse_id, "amount": "2000.00"}, headers=headers
    )
    payout_id = create.json()["data"]["id"]

    admin_headers = await _admin_platform_headers(db_session)
    await client.post(f"/payouts/{payout_id}/approve", headers=admin_headers)

    detail = await client.get(f"/payouts/{payout_id}", headers=headers)
    transfer_ref = detail.json()["data"]["monnify_transfer_ref"]

    import hashlib
    import hmac
    import json

    from app.core.config import settings

    body = json.dumps(
        {
            "eventType": "FAILED_DISBURSEMENT",
            "eventData": {
                "transactionReference": f"MNFY|{transfer_ref}",
                "reference": transfer_ref,
                # Real Monnify FAILED_DISBURSEMENT events carry the failure
                # explanation in transactionDescription, not a "reason" key.
                "transactionDescription": "insufficient funds in wallet",
            },
        }
    ).encode()
    sig = hmac.new(settings.MONNIFY_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()

    resp = await client.post("/webhooks/monnify/transfers", content=body, headers={"monnify-signature": sig})
    assert resp.status_code == 202

    final = await client.get(f"/payouts/{payout_id}", headers=headers)
    assert final.json()["data"]["status"] == "failed"
    assert final.json()["data"]["failure_reason"] == "insufficient funds in wallet"

    # Money never left -- the balance the failed payout had committed is now free again
    # for a new request (this system has no "retry the same payout" endpoint; the rep
    # is expected to submit a fresh request, per the edge-case table in the guide).
    balance = await client.get(f"/purses/{purse_id}/available-balance", headers=headers)
    assert balance.json()["data"]["available_balance"] == "2500.00"

    retry = await client.post(
        "/payouts", json={"group_id": str(group.id), "purse_id": purse_id, "amount": "2000.00"}, headers=headers
    )
    assert retry.status_code == 201


async def test_payout_transfer_initiation_failure_marks_payout_failed(client, db_session):
    org, group, headers, purse_id = await _setup_purse_with_paid_contribution(client, db_session, collected="2500.00")
    await _register_settlement_account(client, group.id, headers)
    _state["monnify"].transfer_should_fail = True

    create = await client.post(
        "/payouts", json={"group_id": str(group.id), "purse_id": purse_id, "amount": "2000.00"}, headers=headers
    )
    payout_id = create.json()["data"]["id"]

    admin_headers = await _admin_platform_headers(db_session)
    await client.post(f"/payouts/{payout_id}/approve", headers=admin_headers)

    final = await client.get(f"/payouts/{payout_id}", headers=headers)
    assert final.json()["data"]["status"] == "failed"


async def test_sweep_payout_allocates_proportionally_across_purses(client, db_session):
    org, group, headers, purse_a = await _setup_purse_with_paid_contribution(
        client, db_session, collected="1000.00", email="sweep-rep@example.com"
    )
    purse_b = await _add_purse_with_collected_amount(client, db_session, headers, "2000.00", "b")
    purse_c = await _add_purse_with_collected_amount(client, db_session, headers, "3000.00", "c")
    # group total collected = 1000 + 2000 + 3000 = 6000.00

    sweep = await client.post(
        "/payouts", json={"group_id": str(group.id), "amount": "3000.00"}, headers=headers
    )
    assert sweep.status_code == 201, sweep.text

    balance_a = (await client.get(f"/purses/{purse_a}/available-balance", headers=headers)).json()["data"]
    balance_b = (await client.get(f"/purses/{purse_b}/available-balance", headers=headers)).json()["data"]
    balance_c = (await client.get(f"/purses/{purse_c}/available-balance", headers=headers)).json()["data"]

    # Proportional to each purse's collected total: a=1000/6000, b=2000/6000, c=3000/6000 of the 3000 swept.
    assert balance_a["available_balance"] == "500.00"
    assert balance_b["available_balance"] == "1000.00"
    assert balance_c["available_balance"] == "1500.00"

    total_available_after_sweep = (
        Decimal(balance_a["available_balance"])
        + Decimal(balance_b["available_balance"])
        + Decimal(balance_c["available_balance"])
    )
    assert total_available_after_sweep + Decimal("3000.00") == Decimal("6000.00")

    # A purse-scoped payout requested after the sweep must account for that
    # purse's prior allocation: purse_c has 1500.00 left, not its full 3000.00.
    within = await client.post(
        "/payouts", json={"group_id": str(group.id), "purse_id": purse_c, "amount": "1500.00"}, headers=headers
    )
    assert within.status_code == 201, within.text

    over = await client.post(
        "/payouts", json={"group_id": str(group.id), "purse_id": purse_c, "amount": "0.01"}, headers=headers
    )
    assert over.status_code == 422
    assert over.json()["error"]["code"] == "insufficient_balance"


async def test_payout_approve_requires_platform_admin(client, db_session):
    org, group, headers, purse_id = await _setup_purse_with_paid_contribution(client, db_session, collected="2500.00")

    create = await client.post(
        "/payouts", json={"group_id": str(group.id), "purse_id": purse_id, "amount": "2000.00"}, headers=headers
    )
    payout_id = create.json()["data"]["id"]

    resp = await client.post(f"/payouts/{payout_id}/approve", headers=headers)
    assert resp.status_code == 403


async def test_member_cannot_list_or_view_payouts(client, db_session):
    org, group, headers, purse_id = await _setup_purse_with_paid_contribution(client, db_session, collected="2500.00")

    create = await client.post(
        "/payouts", json={"group_id": str(group.id), "purse_id": purse_id, "amount": "2000.00"}, headers=headers
    )
    payout_id = create.json()["data"]["id"]

    invite = await client.post("/group-admins/invite-links", json={"expires_in_days": 7}, headers=headers)
    token = invite.json()["data"]["token"]
    await client.post(
        f"/members/join/{token}",
        json={"email": "onlooker@example.com", "password": "password123", "first_name": "On", "last_name": "Looker"},
    )
    login = await client.post("/auth/login", json={"email": "onlooker@example.com", "password": "password123"})
    member_headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}

    list_resp = await client.get("/payouts", headers=member_headers)
    assert list_resp.status_code == 403

    detail_resp = await client.get(f"/payouts/{payout_id}", headers=member_headers)
    assert detail_resp.status_code == 403


async def test_platform_admin_can_list_and_view_all_payouts(client, db_session):
    org, group, headers, purse_id = await _setup_purse_with_paid_contribution(client, db_session, collected="2500.00")

    create = await client.post(
        "/payouts", json={"group_id": str(group.id), "purse_id": purse_id, "amount": "2000.00"}, headers=headers
    )
    payout_id = create.json()["data"]["id"]

    admin_headers = await _admin_platform_headers(db_session)

    list_resp = await client.get("/payouts", headers=admin_headers)
    assert list_resp.status_code == 200
    assert any(p["id"] == payout_id for p in list_resp.json()["data"])

    detail_resp = await client.get(f"/payouts/{payout_id}", headers=admin_headers)
    assert detail_resp.status_code == 200
    assert detail_resp.json()["data"]["id"] == payout_id
