import uuid

from sqlalchemy import select

from app.modules.auth.models import User
from app.modules.members.models import Member
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


async def _create_invite_link(
    client, db_session, org=None, group=None, cohort=None, member_id_format=None, admin_email="rep@example.com"
):
    if org is None or group is None:
        org, group = await create_org_and_group(db_session, member_id_format=member_id_format)
    admin_token = await _register_and_login_group_admin(client, email=admin_email)
    headers = {"Authorization": f"Bearer {admin_token}"}
    await client.post(
        "/group-admins/onboard",
        json={"organization_id": str(org.id), "group_id": str(group.id), "cohort": cohort},
        headers=headers,
    )
    invite = await client.post(
        "/group-admins/invite-links", json={"cohort": cohort, "expires_in_days": 7}, headers=headers
    )
    return invite.json()["data"]["token"], org, group


async def test_resolve_unknown_invite_returns_404(client, db_session):
    resp = await client.get(f"/invites/{uuid.uuid4().hex}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_resolve_invite_success(client, db_session):
    token, org, group = await _create_invite_link(client, db_session, cohort="300L")

    resp = await client.get(f"/invites/{token}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["group"]["short_code"] == group.short_code
    assert body["data"]["organization"]["short_code"] == org.short_code
    assert body["data"]["cohort"] == "300L"


async def test_resolve_invite_includes_member_id_format_when_configured(client, db_session):
    token, org, group = await _create_invite_link(
        client, db_session, member_id_format=r"^\d{2}/[A-Z]{3}/\d{4}$"
    )

    resp = await client.get(f"/invites/{token}")
    assert resp.status_code == 200
    assert resp.json()["data"]["organization"]["member_id_format"] == r"^\d{2}/[A-Z]{3}/\d{4}$"


async def test_resolve_invite_member_id_format_is_null_when_not_configured(client, db_session):
    token, org, group = await _create_invite_link(client, db_session, member_id_format=None)

    resp = await client.get(f"/invites/{token}")
    assert resp.status_code == 200
    assert resp.json()["data"]["organization"]["member_id_format"] is None


async def test_join_success_and_profile_get_update(client, db_session):
    token, org, group = await _create_invite_link(client, db_session, cohort="300L")

    join = await client.post(
        f"/members/join/{token}",
        json={
            "email": "ada@example.com",
            "password": "password123",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "member_id_number": "20/CSC/1234",
        },
    )
    assert join.status_code == 201
    body = join.json()
    assert body["success"] is True
    assert body["data"]["group_id"] == str(group.id)
    assert body["data"]["cohort"] == "300L"
    assert body["data"]["verification_status"] == "pending"

    login = await client.post(
        "/auth/login", json={"email": "ada@example.com", "password": "password123"}
    )
    member_headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}

    me = await client.get("/members/me", headers=member_headers)
    assert me.status_code == 200
    assert me.json()["data"]["group"]["id"] == str(group.id)

    update = await client.patch(
        "/members/me", json={"member_id_number": "21/CSC/9999"}, headers=member_headers
    )
    assert update.status_code == 200
    assert update.json()["data"]["member_id_number"] == "21/CSC/9999"


async def test_join_with_null_cohort_when_group_has_none(client, db_session):
    # A company Group with no cohort at all -- cohort must stay optional end-to-end.
    org, group = await create_org_and_group(
        db_session, org_name="Acme Inc", org_short_code="ACME", group_name="Engineering", group_short_code="ENG"
    )
    token, _, _ = await _create_invite_link(client, db_session, org=org, group=group, cohort=None)

    join = await client.post(
        f"/members/join/{token}",
        json={
            "email": "worker@example.com",
            "password": "password123",
            "first_name": "Worker",
            "last_name": "Bee",
        },
    )
    assert join.status_code == 201
    assert join.json()["data"]["cohort"] is None


async def test_join_rejects_member_id_number_not_matching_org_format(client, db_session):
    token, org, group = await _create_invite_link(
        client, db_session, member_id_format=r"^\d{2}/[A-Z]{3}/\d{4}$"
    )

    resp = await client.post(
        f"/members/join/{token}",
        json={
            "email": "badformat@example.com",
            "password": "password123",
            "first_name": "Bad",
            "last_name": "Format",
            "member_id_number": "not-the-right-shape",
        },
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "member_id_format_mismatch"


async def test_join_skips_validation_when_org_has_no_format_configured(client, db_session):
    token, org, group = await _create_invite_link(client, db_session, member_id_format=None)

    resp = await client.post(
        f"/members/join/{token}",
        json={
            "email": "anyformat@example.com",
            "password": "password123",
            "first_name": "Any",
            "last_name": "Format",
            "member_id_number": "literally-anything-goes",
        },
    )
    assert resp.status_code == 201


async def test_join_with_revoked_invite_returns_410(client, db_session):
    org, group = await create_org_and_group(db_session)
    admin_token = await _register_and_login_group_admin(client)
    headers = {"Authorization": f"Bearer {admin_token}"}
    await client.post(
        "/group-admins/onboard",
        json={"organization_id": str(org.id), "group_id": str(group.id)},
        headers=headers,
    )
    invite = await client.post(
        "/group-admins/invite-links", json={"expires_in_days": 7}, headers=headers
    )
    invite_id = invite.json()["data"]["id"]
    token = invite.json()["data"]["token"]

    await client.delete(f"/group-admins/invite-links/{invite_id}", headers=headers)

    resp = await client.get(f"/invites/{token}")
    assert resp.status_code == 410
    assert resp.json()["error"]["code"] == "invite_exhausted"


async def test_group_admin_can_list_members_who_joined_via_invite(client, db_session):
    org, group = await create_org_and_group(db_session)
    admin_token = await _register_and_login_group_admin(client)
    headers = {"Authorization": f"Bearer {admin_token}"}
    await client.post(
        "/group-admins/onboard",
        json={"organization_id": str(org.id), "group_id": str(group.id)},
        headers=headers,
    )
    invite = await client.post(
        "/group-admins/invite-links", json={"expires_in_days": 7}, headers=headers
    )
    token = invite.json()["data"]["token"]

    await client.post(
        f"/members/join/{token}",
        json={
            "email": "traceable@example.com",
            "password": "password123",
            "first_name": "Traceable",
            "last_name": "Member",
        },
    )

    members = await client.get("/group-admins/members", headers=headers)
    assert members.status_code == 200
    assert len(members.json()["data"]) == 1
    assert members.json()["data"][0]["name"] == "Traceable Member"
    assert members.json()["data"][0]["invite_source"] is not None


async def test_join_anonymous_rejects_existing_email_even_for_a_different_group(client, db_session):
    # The anonymous email+password endpoint never proves who's actually
    # submitting the request -- an existing account (already a member of
    # group A) must not be silently attached to group B just because
    # someone typed its email address into this form.
    token_a, org_a, group_a = await _create_invite_link(client, db_session)
    await client.post(
        f"/members/join/{token_a}",
        json={
            "email": "multi-group@example.com",
            "password": "password123",
            "first_name": "Multi",
            "last_name": "Group",
        },
    )

    org_b, group_b = await create_org_and_group(
        db_session, org_name="Other Uni", org_short_code="OU4", group_name="Other Dept", group_short_code="OD4"
    )
    token_b, _, _ = await _create_invite_link(client, db_session, org=org_b, group=group_b)

    resp = await client.post(
        f"/members/join/{token_b}",
        json={
            "email": "multi-group@example.com",
            "password": "some-other-password",
            "first_name": "Attacker",
            "last_name": "Supplied",
        },
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "duplicate_email"


async def test_join_additional_group_same_group_twice_still_409s(client, db_session):
    token, org, group = await _create_invite_link(client, db_session)
    join = await client.post(
        f"/members/join/{token}",
        json={
            "email": "same-group-twice@example.com",
            "password": "password123",
            "first_name": "Same",
            "last_name": "Group",
        },
    )
    assert join.status_code == 201

    login = await client.post(
        "/auth/login", json={"email": "same-group-twice@example.com", "password": "password123"}
    )
    member_headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}

    # A fresh invite link for the SAME group -- already a member there.
    admin_token = await _register_and_login_group_admin(client, email="rep2@example.com")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    await client.post(
        "/group-admins/onboard",
        json={"organization_id": str(org.id), "group_id": str(group.id)},
        headers=admin_headers,
    )
    another_invite = await client.post(
        "/group-admins/invite-links", json={"expires_in_days": 7}, headers=admin_headers
    )
    another_token = another_invite.json()["data"]["token"]

    resp = await client.post(
        f"/members/join-additional/{another_token}", json={}, headers=member_headers
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "duplicate_email"


async def test_join_additional_group_succeeds_for_a_different_group(client, db_session):
    token_a, org_a, group_a = await _create_invite_link(client, db_session)
    join = await client.post(
        f"/members/join/{token_a}",
        json={
            "email": "two-groups@example.com",
            "password": "password123",
            "first_name": "Two",
            "last_name": "Groups",
        },
    )
    assert join.status_code == 201

    login = await client.post(
        "/auth/login", json={"email": "two-groups@example.com", "password": "password123"}
    )
    member_headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}

    org_b, group_b = await create_org_and_group(
        db_session, org_name="Second Uni", org_short_code="SU5", group_name="Second Dept", group_short_code="SD5"
    )
    token_b, _, _ = await _create_invite_link(
        client, db_session, org=org_b, group=group_b, cohort="200", admin_email="rep-b@example.com"
    )

    resp = await client.post(
        f"/members/join-additional/{token_b}",
        json={"member_id_number": "SD5/2024/0001"},
        headers=member_headers,
    )
    assert resp.status_code == 201
    body = resp.json()["data"]
    assert body["group_id"] == str(group_b.id)
    assert body["cohort"] == "200"
    assert body["verification_status"] == "pending"

    users = (
        await db_session.execute(select(User).where(User.email == "two-groups@example.com"))
    ).scalars().all()
    assert len(users) == 1

    members = (
        await db_session.execute(select(Member).where(Member.user_id == users[0].id))
    ).scalars().all()
    assert len(members) == 2
    assert {m.group_id for m in members} == {group_a.id, group_b.id}


async def test_join_additional_group_allows_a_group_admin_account_too(client, db_session):
    admin_token = await _register_and_login_group_admin(client, email="admin-also-member@example.com")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    org, group = await create_org_and_group(db_session)
    await client.post(
        "/group-admins/onboard",
        json={"organization_id": str(org.id), "group_id": str(group.id)},
        headers=admin_headers,
    )

    other_org, other_group = await create_org_and_group(
        db_session, org_name="Third Uni", org_short_code="TU6", group_name="Third Dept", group_short_code="TD6"
    )
    token, _, _ = await _create_invite_link(
        client, db_session, org=other_org, group=other_group, admin_email="rep-c@example.com"
    )

    resp = await client.post(
        "/members/join-additional/" + token, json={}, headers=admin_headers
    )
    assert resp.status_code == 201
    assert resp.json()["data"]["group_id"] == str(other_group.id)


async def test_members_me_purses_includes_contribution_id(client, db_session):
    token, org, group = await _create_invite_link(client, db_session)
    join = await client.post(
        f"/members/join/{token}",
        json={
            "email": "contribid@example.com",
            "password": "password123",
            "first_name": "Contrib",
            "last_name": "Id",
        },
    )
    assert join.status_code == 201

    # Consume the member's own verify-email token before registering the
    # group admin below -- otherwise two unconsumed tokens coexist in redis
    # and find_redis_token (keys[0]) can't reliably tell them apart.
    member_verify_token = await find_redis_token("verify_email")
    await client.post(
        "/auth/verify-email", json={"email": "contribid@example.com", "token": member_verify_token}
    )

    login = await client.post(
        "/auth/login", json={"email": "contribid@example.com", "password": "password123"}
    )
    member_headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}

    from datetime import datetime, timedelta, timezone

    admin_token = await _register_and_login_group_admin(client, email="rep3@example.com")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    await client.post(
        "/group-admins/onboard",
        json={"organization_id": str(org.id), "group_id": str(group.id)},
        headers=admin_headers,
    )
    await client.post(
        "/purses",
        json={
            "title": "Dues",
            "amount": "500.00",
            "deadline": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
            "enroll_mode": "snapshot",
        },
        headers=admin_headers,
    )

    purses = await client.get("/members/me/purses", headers=member_headers)
    assert purses.status_code == 200
    body = purses.json()["data"]
    assert len(body) == 1
    assert "contribution_id" in body[0]

    contribution = await client.get(
        f"/contributions/{body[0]['contribution_id']}", headers=member_headers
    )
    assert contribution.status_code == 200
    assert contribution.json()["data"]["purse_id"] == body[0]["purse_id"]
