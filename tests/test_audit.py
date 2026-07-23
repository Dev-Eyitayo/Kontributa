import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.modules.audit.service import AuditService
from tests.conftest import create_org_and_group, find_redis_token


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


async def _setup_purse_with_paid_contribution(client, db_session, email="rep@example.com"):
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
    member_email = f"member-{email}"
    member_token_resp = await client.post(
        f"/members/join/{token}",
        json={"email": member_email, "password": "password123", "first_name": "Member", "last_name": "One"},
    )
    assert member_token_resp.status_code in (200, 201), member_token_resp.text
    member_login = await client.post("/auth/login", json={"email": member_email, "password": "password123"})
    member_headers = {"Authorization": f"Bearer {member_login.json()['data']['access_token']}"}

    create = await client.post(
        "/purses",
        json={"title": "Fee", "amount": "2500.00", "deadline": _future_deadline(), "enroll_mode": "snapshot"},
        headers=headers,
    )
    purse_id = create.json()["data"]["id"]

    from sqlalchemy import select

    from app.modules.contributions.models import Contribution

    result = await db_session.execute(select(Contribution).where(Contribution.purse_id == purse_id))
    contribution = result.scalar_one()

    mark = await client.post(
        f"/contributions/{contribution.id}/mark-manual",
        json={"amount_received": "2500.00", "note": "cash collected"},
        headers=headers,
    )
    assert mark.status_code == 200, mark.text

    return org, group, headers, member_headers, purse_id, str(contribution.id)


async def test_contribution_audit_visible_to_own_member_and_own_group_rep(client, db_session):
    org, group, headers, member_headers, purse_id, contribution_id = await _setup_purse_with_paid_contribution(
        client, db_session
    )

    rep_view = await client.get(f"/audit/contributions/{contribution_id}", headers=headers)
    assert rep_view.status_code == 200
    entries = rep_view.json()["data"]
    assert len(entries) == 1
    assert entries[0]["from_status"] == "pending"
    assert entries[0]["to_status"] == "paid_manual"
    assert entries[0]["actor_type"] == "group_admin"

    member_view = await client.get(f"/audit/contributions/{contribution_id}", headers=member_headers)
    assert member_view.status_code == 200
    assert len(member_view.json()["data"]) == 1


async def test_contribution_audit_forbidden_for_other_member_and_other_group_rep(client, db_session):
    org, group, headers, member_headers, purse_id, contribution_id = await _setup_purse_with_paid_contribution(
        client, db_session, email="rep-a@example.com"
    )

    other_org, other_group = await create_org_and_group(
        db_session, org_name="Other Uni", org_short_code="OU9", group_name="Other Dept", group_short_code="OD9"
    )
    other_admin_token = await _register_and_login_group_admin(client, email="rep-b@example.com")
    other_headers = {"Authorization": f"Bearer {other_admin_token}"}
    await client.post(
        "/group-admins/onboard",
        json={"organization_id": str(other_org.id), "group_id": str(other_group.id)},
        headers=other_headers,
    )

    resp = await client.get(f"/audit/contributions/{contribution_id}", headers=other_headers)
    assert resp.status_code == 403

    # A second, unrelated member (in the same group) must not see this contribution either.
    invite = await client.post("/group-admins/invite-links", json={"expires_in_days": 7}, headers=headers)
    token = invite.json()["data"]["token"]
    await client.post(
        f"/members/join/{token}",
        json={"email": "second-member@example.com", "password": "password123", "first_name": "Second", "last_name": "Member"},
    )
    second_login = await client.post(
        "/auth/login", json={"email": "second-member@example.com", "password": "password123"}
    )
    second_headers = {"Authorization": f"Bearer {second_login.json()['data']['access_token']}"}

    resp2 = await client.get(f"/audit/contributions/{contribution_id}", headers=second_headers)
    assert resp2.status_code == 403


async def test_purse_audit_includes_edits_and_tied_contribution_events(client, db_session):
    org, group, headers, member_headers, purse_id, contribution_id = await _setup_purse_with_paid_contribution(
        client, db_session
    )

    edit = await client.patch(f"/purses/{purse_id}", json={"amount": "3000.00"}, headers=headers)
    assert edit.status_code == 200, edit.text

    resp = await client.get(f"/audit/purses/{purse_id}", headers=headers)
    assert resp.status_code == 200
    entries = resp.json()["data"]
    entity_types = {e["entity_type"] for e in entries}
    assert "contribution" in entity_types
    assert "purse" in entity_types

    purse_entries = [e for e in entries if e["entity_type"] == "purse"]
    assert purse_entries[0]["action"] == "purse_edited"
    assert purse_entries[0]["before_state"]["amount"] == "2500.00"
    assert purse_entries[0]["after_state"]["amount"] == "3000.00"


async def test_purse_audit_scoped_to_own_group(client, db_session):
    org, group, headers, member_headers, purse_id, contribution_id = await _setup_purse_with_paid_contribution(
        client, db_session, email="rep-c@example.com"
    )

    other_org, other_group = await create_org_and_group(
        db_session, org_name="Other Uni", org_short_code="OU8", group_name="Other Dept", group_short_code="OD8"
    )
    other_admin_token = await _register_and_login_group_admin(client, email="rep-d@example.com")
    other_headers = {"Authorization": f"Bearer {other_admin_token}"}
    await client.post(
        "/group-admins/onboard",
        json={"organization_id": str(other_org.id), "group_id": str(other_group.id)},
        headers=other_headers,
    )

    resp = await client.get(f"/audit/purses/{purse_id}", headers=other_headers)
    assert resp.status_code == 403


async def test_group_audit_feed_is_admin_only(client, db_session):
    from app.core.auth import create_access_token
    from tests.conftest import create_platform_admin

    org, group, headers, member_headers, purse_id, contribution_id = await _setup_purse_with_paid_contribution(
        client, db_session
    )

    rep_attempt = await client.get(f"/audit/groups/{group.id}", headers=headers)
    assert rep_attempt.status_code == 403

    admin = await create_platform_admin(db_session)
    token = create_access_token(admin.id, "group_admin")
    admin_headers = {"Authorization": f"Bearer {token.token}"}

    admin_view = await client.get(f"/audit/groups/{group.id}", headers=admin_headers)
    assert admin_view.status_code == 200
    entity_types = {e["entity_type"] for e in admin_view.json()["data"]["items"]}
    assert "contribution" in entity_types


async def test_group_audit_feed_pagination(client, db_session):
    from app.core.auth import create_access_token
    from tests.conftest import create_platform_admin

    org, group, headers, member_headers, purse_id, contribution_id = await _setup_purse_with_paid_contribution(
        client, db_session
    )

    admin = await create_platform_admin(db_session)
    token = create_access_token(admin.id, "group_admin")
    admin_headers = {"Authorization": f"Bearer {token.token}"}

    full = await client.get(f"/audit/groups/{group.id}", headers=admin_headers)
    total = full.json()["data"]["total"]
    assert total >= 1

    limited = await client.get(f"/audit/groups/{group.id}?limit=1&offset=0", headers=admin_headers)
    body = limited.json()["data"]
    assert body["total"] == total
    assert body["limit"] == 1
    assert len(body["items"]) == 1


async def test_verify_chain_detects_a_directly_inserted_tampered_row(client, db_session):
    org, group, headers, member_headers, purse_id, contribution_id = await _setup_purse_with_paid_contribution(
        client, db_session
    )

    service = AuditService(db_session)
    result = await service.verify_chain()
    assert result["valid"] is True

    # Simulate an attacker/bug bypassing the application layer entirely --
    # a raw SQL INSERT with a row_hash that doesn't match its own content,
    # exactly what happens if you don't know (or don't bother computing)
    # the real hash chain.
    import uuid

    fake_id = uuid.uuid4()
    await db_session.execute(
        text(
            """
            INSERT INTO audit_log
                (id, entity_type, entity_id, action, actor_type, actor_id, before_state, after_state, prev_hash, row_hash, created_at)
            VALUES
                (:id, 'contribution', :entity_id, 'forged_entry', 'webhook', NULL, NULL, NULL, NULL, :row_hash, now())
            """
        ),
        {"id": fake_id, "entity_id": uuid.uuid4(), "row_hash": hashlib.sha256(b"not-the-real-hash").hexdigest()},
    )
    await db_session.commit()

    tampered_result = await service.verify_chain()
    assert tampered_result["valid"] is False
    assert tampered_result["broken_at_id"] == str(fake_id)


async def test_app_role_cannot_update_or_delete_audit_log(client, db_session):
    """Constraint from the phase 6 prompt: this must fail with a real
    database permissions error using the application's own role/connection
    -- not merely because the ORM layer refuses to expose an update/delete
    method. db_session runs as the same restricted RUNTIME_DATABASE_URL
    role the app itself queries as (see conftest.py's db_setup fixture)."""
    org, group, headers, member_headers, purse_id, contribution_id = await _setup_purse_with_paid_contribution(
        client, db_session
    )

    with pytest.raises(DBAPIError) as update_exc:
        await db_session.execute(text("UPDATE audit_log SET action = 'tampered'"))
    await db_session.rollback()
    assert "permission denied" in str(update_exc.value).lower()

    with pytest.raises(DBAPIError) as delete_exc:
        await db_session.execute(text("DELETE FROM audit_log"))
    await db_session.rollback()
    assert "permission denied" in str(delete_exc.value).lower()


async def test_payout_status_transitions_are_queryable_via_audit(client, db_session):
    from app.core.auth import create_access_token
    from tests.conftest import create_platform_admin

    org, group, headers, member_headers, purse_id, contribution_id = await _setup_purse_with_paid_contribution(
        client, db_session
    )

    create = await client.post(
        "/payouts", json={"group_id": str(group.id), "purse_id": purse_id, "amount": "1000.00"}, headers=headers
    )
    payout_id = create.json()["data"]["id"]

    admin = await create_platform_admin(db_session)
    token = create_access_token(admin.id, "group_admin")
    admin_headers = {"Authorization": f"Bearer {token.token}"}

    reject = await client.post(
        f"/payouts/{payout_id}/reject", json={"reason": "test"}, headers=admin_headers
    )
    assert reject.status_code == 200

    resp = await client.get(f"/audit/payouts/{payout_id}", headers=headers)
    assert resp.status_code == 200
    entries = resp.json()["data"]
    assert len(entries) == 1
    assert entries[0]["from_status"] == "requested"
    assert entries[0]["to_status"] == "rejected"
