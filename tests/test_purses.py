from datetime import datetime, timedelta, timezone

from app.core.auth import create_access_token
from tests.conftest import create_org_and_group, create_platform_admin, find_redis_token, onboard_group_admin


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


async def _register_and_login_member(client, token, email, first_name="Member", last_name="One"):
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


async def _setup_group_with_admin(client, db_session, cohort=None):
    org, _existing_group = await create_org_and_group(db_session)
    admin_token = await _register_and_login_group_admin(client)
    headers = {"Authorization": f"Bearer {admin_token}"}
    group = await onboard_group_admin(client, db_session, org, headers, cohort=cohort)
    return org, group, headers


async def _invite_and_join_member(client, headers, group_id, cohort=None, email="member1@example.com"):
    invite = await client.post(
        f"/group-admins/invite-links?group_id={group_id}",
        json={"cohort": cohort, "expires_in_days": 7},
        headers=headers,
    )
    token = invite.json()["data"]["token"]
    member_token = await _register_and_login_member(client, token, email)
    return {"Authorization": f"Bearer {member_token}"}


def _purse_payload(group_id, **overrides):
    payload = {
        "group_id": str(group_id),
        "title": "Fee",
        "amount": "500.00",
        "deadline": _future_deadline(),
        "enroll_mode": "snapshot",
    }
    payload.update(overrides)
    return payload


async def test_create_purse_generates_pending_contributions_for_existing_members(client, db_session):
    org, group, headers = await _setup_group_with_admin(client, db_session)
    member_headers = await _invite_and_join_member(client, headers, group.id)

    create = await client.post(
        "/purses",
        json=_purse_payload(group.id, title="Project Defense Fee", amount="2500.00"),
        headers=headers,
    )
    assert create.status_code == 201
    body = create.json()
    assert body["success"] is True
    purse_id = body["data"]["id"]
    assert body["data"]["status"] == "open"

    contributions = await client.get(f"/purses/{purse_id}/contributions", headers=headers)
    assert contributions.status_code == 200
    rows = contributions.json()["data"]["items"]
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert rows[0]["amount_received"] == "0.00"
    # transparency view must never leak more than name + member_id_number

    assert set(rows[0].keys()) == {
        "id",
        "member_id",
        "name",
        "member_id_number",
        "status",
        "amount_received",
        "paid_at",
    }

    member_purses = await client.get("/members/me/purses", headers=member_headers)
    assert member_purses.status_code == 200
    assert member_purses.json()["data"][0]["purse_id"] == purse_id
    assert member_purses.json()["data"][0]["contribution_status"] == "pending"
    assert member_purses.json()["data"][0]["group"]["id"] == str(group.id)


async def test_snapshot_purse_does_not_include_latecomers(client, db_session):
    org, group, headers = await _setup_group_with_admin(client, db_session)
    await _invite_and_join_member(client, headers, group.id, email="early@example.com")

    create = await client.post(
        "/purses",
        json=_purse_payload(group.id, title="Snapshot Fee", amount="1000.00"),
        headers=headers,
    )
    purse_id = create.json()["data"]["id"]

    # A member joining after a snapshot purse was created must NOT get a contribution.
    late_member_headers = await _invite_and_join_member(client, headers, group.id, email="late@example.com")

    summary = await client.get(f"/purses/{purse_id}/summary", headers=headers)
    assert summary.json()["data"]["pending_count"] == 1

    late_purses = await client.get("/members/me/purses", headers=late_member_headers)
    assert late_purses.json()["data"] == []


async def test_auto_enroll_purse_includes_future_joiners(client, db_session):
    org, group, headers = await _setup_group_with_admin(client, db_session)
    await _invite_and_join_member(client, headers, group.id, email="early@example.com")

    create = await client.post(
        "/purses",
        json=_purse_payload(group.id, title="Auto Enroll Fee", amount="1000.00", enroll_mode="auto_enroll"),
        headers=headers,
    )
    purse_id = create.json()["data"]["id"]

    late_member_headers = await _invite_and_join_member(client, headers, group.id, email="late@example.com")

    summary = await client.get(f"/purses/{purse_id}/summary", headers=headers)
    assert summary.json()["data"]["pending_count"] == 2

    late_purses = await client.get("/members/me/purses", headers=late_member_headers)
    assert late_purses.json()["data"][0]["purse_id"] == purse_id


async def test_enroll_mode_is_immutable(client, db_session):
    org, group, headers = await _setup_group_with_admin(client, db_session)
    create = await client.post(
        "/purses",
        json=_purse_payload(group.id, title="Immutable Mode Fee"),
        headers=headers,
    )
    purse_id = create.json()["data"]["id"]

    # enroll_mode isn't even an accepted field on PATCH -- extra fields are ignored by pydantic,
    # so the purse must remain snapshot no matter what's sent.
    patch = await client.patch(
        f"/purses/{purse_id}", json={"enroll_mode": "auto_enroll", "amount": "600.00"}, headers=headers
    )
    assert patch.status_code == 200

    detail = await client.get(f"/purses/{purse_id}", headers=headers)
    assert detail.json()["data"]["enroll_mode"] == "snapshot"


async def test_editing_amount_only_updates_pending_contributions(client, db_session):
    """The single easiest rule to get wrong: already-paid contributions must
    keep their original amount_expected when a purse's amount is edited."""
    org, group, headers = await _setup_group_with_admin(client, db_session)
    member_headers_1 = await _invite_and_join_member(client, headers, group.id, email="payer@example.com")
    member_headers_2 = await _invite_and_join_member(client, headers, group.id, email="nonpayer@example.com")

    create = await client.post(
        "/purses",
        json=_purse_payload(group.id, title="Amount Edit Fee", amount="1000.00"),
        headers=headers,
    )
    purse_id = create.json()["data"]["id"]

    contributions_before = await client.get(f"/purses/{purse_id}/contributions", headers=headers)
    paid_member_id = contributions_before.json()["data"]["items"][0]["member_id"]

    # Directly flip one contribution to paid via the DB, since Monnify integration doesn't exist yet.
    from sqlalchemy import select

    from app.modules.contributions.models import Contribution, ContributionStatus

    result = await db_session.execute(
        select(Contribution).where(Contribution.member_id == paid_member_id, Contribution.purse_id == purse_id)
    )
    contribution = result.scalar_one()
    contribution.status = ContributionStatus.PAID
    contribution.amount_received = contribution.amount_expected
    await db_session.commit()

    patch = await client.patch(f"/purses/{purse_id}", json={"amount": "1500.00"}, headers=headers)
    assert patch.status_code == 200
    assert patch.json()["data"]["amount"] == "1500.00"

    contributions_after = await client.get(f"/purses/{purse_id}/contributions", headers=headers)
    rows = {r["member_id"]: r for r in contributions_after.json()["data"]["items"]}

    assert rows[paid_member_id]["status"] == "paid"
    assert rows[paid_member_id]["amount_received"] == "1000.00"

    pending_row = [r for mid, r in rows.items() if mid != paid_member_id][0]
    assert pending_row["status"] == "pending"


async def test_edit_purse_amount_must_be_positive(client, db_session):
    org, group, headers = await _setup_group_with_admin(client, db_session)
    create = await client.post(
        "/purses",
        json=_purse_payload(group.id, title="Validation Fee"),
        headers=headers,
    )
    purse_id = create.json()["data"]["id"]

    resp = await client.patch(f"/purses/{purse_id}", json={"amount": "-5.00"}, headers=headers)
    assert resp.status_code == 422


async def test_cannot_edit_closed_purse(client, db_session):
    org, group, headers = await _setup_group_with_admin(client, db_session)
    create = await client.post(
        "/purses",
        json=_purse_payload(group.id, title="Closeable Fee"),
        headers=headers,
    )
    purse_id = create.json()["data"]["id"]

    close = await client.post(f"/purses/{purse_id}/close", headers=headers)
    assert close.status_code == 200
    assert close.json()["data"]["status"] == "closed"

    resp = await client.patch(f"/purses/{purse_id}", json={"amount": "600.00"}, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "purse_not_open"


async def test_rep_cannot_manage_another_reps_purse(client, db_session):
    org, group, headers_a = await _setup_group_with_admin(client, db_session)

    org_b, _existing_group_b = await create_org_and_group(
        db_session, org_name="Other Uni", org_short_code="OU2", group_name="Other Dept", group_short_code="OD2"
    )
    admin_b_token = await _register_and_login_group_admin(client, email="repB@example.com")
    headers_b = {"Authorization": f"Bearer {admin_b_token}"}
    await onboard_group_admin(client, db_session, org_b, headers_b, group_name="Other Group B")

    create = await client.post(
        "/purses",
        json=_purse_payload(group.id, title="A's Fee"),
        headers=headers_a,
    )
    purse_id = create.json()["data"]["id"]

    forbidden = await client.patch(f"/purses/{purse_id}", json={"amount": "600.00"}, headers=headers_b)
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "forbidden"

    forbidden_contributions = await client.get(f"/purses/{purse_id}/contributions", headers=headers_b)
    assert forbidden_contributions.status_code == 403
    assert forbidden_contributions.json()["error"]["code"] == "forbidden"


async def test_platform_admin_can_list_contributions_for_any_purse(client, db_session):
    # A platform admin has no GroupAdmin profile row, so this endpoint
    # must not 404/403 for them the way it correctly does for an
    # unrelated group admin (see the test above).
    org, group, headers = await _setup_group_with_admin(client, db_session)
    create = await client.post(
        "/purses",
        json=_purse_payload(group.id, title="Cross-group visible fee"),
        headers=headers,
    )
    purse_id = create.json()["data"]["id"]

    admin = await create_platform_admin(db_session)
    token = create_access_token(admin.id, "group_admin")
    admin_headers = {"Authorization": f"Bearer {token.token}"}

    resp = await client.get(f"/purses/{purse_id}/contributions", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["success"] is True


async def test_same_group_admin_can_manage_purse_they_did_not_create(client, db_session):
    org, group, headers_a = await _setup_group_with_admin(client, db_session)

    admin_b_token = await _register_and_login_group_admin(client, email="repB-sameteam@example.com")
    headers_b = {"Authorization": f"Bearer {admin_b_token}"}
    await client.post(
        "/group-admins/onboard",
        json={"organization_id": str(org.id), "new_group_name": "Second Admin's Own Group"},
        headers=headers_b,
    )
    # headers_b now administers a *different* group than group -- to prove
    # "same group, co-admin" access, headers_b must be given a GroupAdmin
    # row on `group` itself (there is no API path for that -- co-admin
    # invites are out of scope -- see known-limitations.md), so this is
    # set up directly in the DB instead.
    from app.modules.group_admins.models import GroupAdmin
    from app.modules.auth.models import User
    from sqlalchemy import select

    admin_b_email = "repB-sameteam@example.com"
    user_b = (await db_session.execute(select(User).where(User.email == admin_b_email))).scalar_one()
    db_session.add(GroupAdmin(user_id=user_b.id, group_id=group.id))
    await db_session.commit()

    create = await client.post(
        "/purses",
        json=_purse_payload(group.id, title="A's Fee"),
        headers=headers_a,
    )
    purse_id = create.json()["data"]["id"]

    # Purse belongs to the group, not the creating admin -- a co-admin in
    # the same group can edit and close it, matching "a new rep inherits
    # full visibility and control, nothing is orphaned."
    edit = await client.patch(f"/purses/{purse_id}", json={"amount": "600.00"}, headers=headers_b)
    assert edit.status_code == 200
    assert edit.json()["data"]["amount"] == "600.00"

    close = await client.post(f"/purses/{purse_id}/close", headers=headers_b)
    assert close.status_code == 200
    assert close.json()["data"]["status"] == "closed"


async def test_create_purse_idempotency_key_prevents_duplicate(client, db_session):
    org, group, headers = await _setup_group_with_admin(client, db_session)
    headers_with_key = {**headers, "Idempotency-Key": "test-key-123"}

    payload = _purse_payload(group.id, title="Idempotent Fee")
    first = await client.post("/purses", json=payload, headers=headers_with_key)
    second = await client.post("/purses", json=payload, headers=headers_with_key)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["data"]["id"] == second.json()["data"]["id"]

    admin_purses = await client.get(f"/purses?group_id={group.id}", headers=headers)
    matching = [p for p in admin_purses.json()["data"]["items"] if p["title"] == "Idempotent Fee"]
    assert len(matching) == 1


async def test_create_purse_idempotency_key_conflict_on_different_body(client, db_session):
    org, group, headers = await _setup_group_with_admin(client, db_session)
    headers_with_key = {**headers, "Idempotency-Key": "reused-key"}

    await client.post(
        "/purses",
        json=_purse_payload(group.id, title="First Fee"),
        headers=headers_with_key,
    )
    conflict = await client.post(
        "/purses",
        json=_purse_payload(group.id, title="Different Fee", amount="999.00"),
        headers=headers_with_key,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_key_conflict"


async def test_purse_list_and_detail_role_aware_shapes(client, db_session):
    org, group, headers = await _setup_group_with_admin(client, db_session)
    member_headers = await _invite_and_join_member(client, headers, group.id)

    create = await client.post(
        "/purses",
        json=_purse_payload(group.id, title="Shape Fee"),
        headers=headers,
    )
    purse_id = create.json()["data"]["id"]

    rep_list = await client.get(f"/purses?group_id={group.id}", headers=headers)
    assert "paid_count" in rep_list.json()["data"]["items"][0]
    assert "total_count" in rep_list.json()["data"]["items"][0]

    member_list = await client.get("/purses", headers=member_headers)
    assert "contribution_status" in member_list.json()["data"]["items"][0]
    assert "paid_count" not in member_list.json()["data"]["items"][0]

    rep_detail = await client.get(f"/purses/{purse_id}", headers=headers)
    assert rep_detail.json()["data"]["enroll_mode"] == "snapshot"
    assert "paid_count" in rep_detail.json()["data"]

    member_detail = await client.get(f"/purses/{purse_id}", headers=member_headers)
    assert member_detail.json()["data"]["contribution_status"] == "pending"


async def test_purse_specific_invite_grants_eligibility_even_for_snapshot_purse(client, db_session):
    org, group, headers = await _setup_group_with_admin(client, db_session)

    create = await client.post(
        "/purses",
        json=_purse_payload(group.id, title="Excursion Fund", amount="3000.00"),
        headers=headers,
    )
    purse_id = create.json()["data"]["id"]

    invite = await client.post(
        f"/group-admins/invite-links?group_id={group.id}",
        json={"purse_id": purse_id, "expires_in_days": 7},
        headers=headers,
    )
    assert invite.status_code == 201
    token = invite.json()["data"]["token"]

    resolved = await client.get(f"/invites/{token}")
    assert resolved.json()["data"]["purse_title"] == "Excursion Fund"

    join = await client.post(
        f"/members/join/{token}",
        json={
            "email": "purse-specific@example.com",
            "password": "password123",
            "first_name": "Late",
            "last_name": "Joiner",
        },
    )
    assert join.status_code == 201
    verify_token = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"email": "purse-specific@example.com", "token": verify_token})
    login = await client.post(
        "/auth/login", json={"email": "purse-specific@example.com", "password": "password123"}
    )
    member_headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}

    # Snapshot purses normally exclude latecomers -- but this invite was scoped
    # directly to the purse, so eligibility must be granted anyway.
    member_purses = await client.get("/members/me/purses", headers=member_headers)
    assert len(member_purses.json()["data"]) == 1
    assert member_purses.json()["data"][0]["purse_id"] == purse_id
    assert member_purses.json()["data"][0]["contribution_status"] == "pending"


async def test_invite_link_rejects_purse_from_another_group(client, db_session):
    org, group, headers = await _setup_group_with_admin(client, db_session)
    other_org, _existing_other_group = await create_org_and_group(
        db_session, org_name="Other Uni", org_short_code="OU3", group_name="Other Dept", group_short_code="OD3"
    )
    other_admin_token = await _register_and_login_group_admin(client, email="other-rep@example.com")
    other_headers = {"Authorization": f"Bearer {other_admin_token}"}
    other_group = await onboard_group_admin(client, db_session, other_org, other_headers, group_name="Other Group")
    other_purse = await client.post(
        "/purses",
        json=_purse_payload(other_group.id, title="Other Fee"),
        headers=other_headers,
    )
    other_purse_id = other_purse.json()["data"]["id"]

    resp = await client.post(
        f"/group-admins/invite-links?group_id={group.id}",
        json={"purse_id": other_purse_id, "expires_in_days": 7},
        headers=headers,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "purse_group_mismatch"


async def test_invite_link_rejects_closed_purse(client, db_session):
    org, group, headers = await _setup_group_with_admin(client, db_session)
    create = await client.post(
        "/purses",
        json=_purse_payload(group.id, title="Closed Fee"),
        headers=headers,
    )
    purse_id = create.json()["data"]["id"]
    await client.post(f"/purses/{purse_id}/close", headers=headers)

    resp = await client.post(
        f"/group-admins/invite-links?group_id={group.id}",
        json={"purse_id": purse_id, "expires_in_days": 7},
        headers=headers,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "purse_not_open"


async def test_admin_can_manually_add_existing_member_to_snapshot_purse(client, db_session):
    org, group, headers = await _setup_group_with_admin(client, db_session)

    create = await client.post(
        "/purses",
        json=_purse_payload(group.id, title="Backfill Fee", amount="750.00"),
        headers=headers,
    )
    purse_id = create.json()["data"]["id"]

    # Member joins the group after the snapshot purse already exists.
    late_member_headers = await _invite_and_join_member(client, headers, group.id, email="late@example.com")
    member_me = await client.get("/members/me", headers=late_member_headers)
    member_id = member_me.json()["data"]["id"]

    # Snapshot purse excludes them by default.
    late_purses = await client.get("/members/me/purses", headers=late_member_headers)
    assert late_purses.json()["data"] == []

    add = await client.post(
        f"/purses/{purse_id}/contributions", json={"member_id": member_id}, headers=headers
    )
    assert add.status_code == 201
    body = add.json()["data"]
    assert body["purse_id"] == purse_id
    assert body["member_id"] == member_id
    assert body["status"] == "pending"
    assert body["amount_expected"] == "750.00"

    late_purses_after = await client.get("/members/me/purses", headers=late_member_headers)
    assert len(late_purses_after.json()["data"]) == 1
    assert late_purses_after.json()["data"][0]["purse_id"] == purse_id


async def test_add_member_to_purse_rejects_duplicate_enrollment(client, db_session):
    org, group, headers = await _setup_group_with_admin(client, db_session)
    member_headers = await _invite_and_join_member(client, headers, group.id, email="already-in@example.com")
    member_me = await client.get("/members/me", headers=member_headers)
    member_id = member_me.json()["data"]["id"]

    create = await client.post(
        "/purses",
        json=_purse_payload(group.id, title="Dues"),
        headers=headers,
    )
    purse_id = create.json()["data"]["id"]

    resp = await client.post(
        f"/purses/{purse_id}/contributions", json={"member_id": member_id}, headers=headers
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "already_enrolled"


async def test_add_member_to_purse_rejects_cohort_mismatch(client, db_session):
    org, group, headers = await _setup_group_with_admin(client, db_session)
    late_member_headers = await _invite_and_join_member(
        client, headers, group.id, cohort="300", email="wrongcohort@example.com"
    )
    member_me = await client.get("/members/me", headers=late_member_headers)
    member_id = member_me.json()["data"]["id"]

    create = await client.post(
        "/purses",
        json=_purse_payload(group.id, title="400L Dues", cohort="400"),
        headers=headers,
    )
    purse_id = create.json()["data"]["id"]

    resp = await client.post(
        f"/purses/{purse_id}/contributions", json={"member_id": member_id}, headers=headers
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "cohort_mismatch"


async def test_add_member_to_purse_rejects_closed_purse(client, db_session):
    org, group, headers = await _setup_group_with_admin(client, db_session)
    late_member_headers = await _invite_and_join_member(client, headers, group.id, email="closedpurse@example.com")
    member_me = await client.get("/members/me", headers=late_member_headers)
    member_id = member_me.json()["data"]["id"]

    create = await client.post(
        "/purses",
        json=_purse_payload(group.id, title="Closed Dues"),
        headers=headers,
    )
    purse_id = create.json()["data"]["id"]
    await client.post(f"/purses/{purse_id}/close", headers=headers)

    resp = await client.post(
        f"/purses/{purse_id}/contributions", json={"member_id": member_id}, headers=headers
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "purse_not_open"


async def test_add_member_to_purse_rejects_member_outside_group(client, db_session):
    org_a, group_a, headers_a = await _setup_group_with_admin(client, db_session)

    org_b, _existing_group_b = await create_org_and_group(
        db_session, org_name="Other Uni", org_short_code="OU3", group_name="Other Dept", group_short_code="OD3"
    )
    admin_b_token = await _register_and_login_group_admin(client, email="repB-outsider@example.com")
    headers_b = {"Authorization": f"Bearer {admin_b_token}"}
    group_b = await onboard_group_admin(client, db_session, org_b, headers_b, group_name="Group B")

    other_member_headers = await _invite_and_join_member(client, headers_b, group_b.id, email="outsider@example.com")
    other_member_me = await client.get("/members/me", headers=other_member_headers)
    other_member_id = other_member_me.json()["data"]["id"]

    create = await client.post(
        "/purses",
        json=_purse_payload(group_a.id, title="Group A Dues"),
        headers=headers_a,
    )
    purse_id = create.json()["data"]["id"]

    resp = await client.post(
        f"/purses/{purse_id}/contributions", json={"member_id": other_member_id}, headers=headers_a
    )
    assert resp.status_code == 404


async def test_purse_list_pagination(client, db_session):
    org, group, headers = await _setup_group_with_admin(client, db_session)

    for i in range(3):
        await client.post(
            "/purses",
            json=_purse_payload(group.id, title=f"Fee {i}"),
            headers=headers,
        )

    first_page = await client.get(f"/purses?group_id={group.id}&limit=2&offset=0", headers=headers)
    assert first_page.status_code == 200
    body = first_page.json()["data"]
    assert body["total"] == 3
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert len(body["items"]) == 2

    second_page = await client.get(f"/purses?group_id={group.id}&limit=2&offset=2", headers=headers)
    body_2 = second_page.json()["data"]
    assert len(body_2["items"]) == 1

    bad_limit = await client.get(f"/purses?group_id={group.id}&limit=0", headers=headers)
    assert bad_limit.status_code == 422


async def test_purse_list_requires_group_id_for_group_admin(client, db_session):
    org, group, headers = await _setup_group_with_admin(client, db_session)
    resp = await client.get("/purses", headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "group_id_required"


async def test_purse_contributions_pagination(client, db_session):
    org, group, headers = await _setup_group_with_admin(client, db_session)
    await _invite_and_join_member(client, headers, group.id, email="pg1@example.com")
    await _invite_and_join_member(client, headers, group.id, email="pg2@example.com")
    await _invite_and_join_member(client, headers, group.id, email="pg3@example.com")

    create = await client.post(
        "/purses",
        json=_purse_payload(group.id, title="Paginated Fee"),
        headers=headers,
    )
    purse_id = create.json()["data"]["id"]

    first_page = await client.get(f"/purses/{purse_id}/contributions?limit=2&offset=0", headers=headers)
    body = first_page.json()["data"]
    assert body["total"] == 3
    assert len(body["items"]) == 2

    second_page = await client.get(f"/purses/{purse_id}/contributions?limit=2&offset=2", headers=headers)
    body_2 = second_page.json()["data"]
    assert len(body_2["items"]) == 1
