import uuid

from sqlalchemy import select

from app.modules.auth.models import User
from app.modules.members.models import Member
from tests.conftest import create_org_and_group, find_redis_token, onboard_group_admin


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
    client, db_session, org=None, cohort=None, member_id_format=None, admin_email="rep@example.com"
):
    """Onboards a brand-new admin (and, with it, a brand-new group -- see
    onboard_group_admin) under `org` (creating a fresh org too if none is
    given), then creates an invite link for that real group. Returns the
    *actual* Group the onboard call created, not any group a caller might
    separately have made -- there's no longer a way to onboard into an
    existing one."""
    if org is None:
        org, _unused_group = await create_org_and_group(db_session, member_id_format=member_id_format)
    admin_token = await _register_and_login_group_admin(client, email=admin_email)
    headers = {"Authorization": f"Bearer {admin_token}"}
    group = await onboard_group_admin(client, db_session, org, headers, cohort=cohort)
    invite = await client.post(
        f"/group-admins/invite-links?group_id={group.id}",
        json={"cohort": cohort, "expires_in_days": 7},
        headers=headers,
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

    verify_token = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"email": "ada@example.com", "token": verify_token})
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
    # A company org with no cohort at all -- cohort must stay optional end-to-end.
    org, _existing_group = await create_org_and_group(
        db_session, org_name="Acme Inc", org_short_code="ACME", group_name="Engineering", group_short_code="ENG"
    )
    token, _, _ = await _create_invite_link(client, db_session, org=org, cohort=None)

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
    org, _existing_group = await create_org_and_group(db_session)
    admin_token = await _register_and_login_group_admin(client)
    headers = {"Authorization": f"Bearer {admin_token}"}
    group = await onboard_group_admin(client, db_session, org, headers)
    invite = await client.post(
        f"/group-admins/invite-links?group_id={group.id}", json={"expires_in_days": 7}, headers=headers
    )
    invite_id = invite.json()["data"]["id"]
    token = invite.json()["data"]["token"]

    await client.delete(f"/group-admins/invite-links/{invite_id}?group_id={group.id}", headers=headers)

    resp = await client.get(f"/invites/{token}")
    assert resp.status_code == 410
    assert resp.json()["error"]["code"] == "invite_exhausted"


async def test_group_admin_can_list_members_who_joined_via_invite(client, db_session):
    org, _existing_group = await create_org_and_group(db_session)
    admin_token = await _register_and_login_group_admin(client)
    headers = {"Authorization": f"Bearer {admin_token}"}
    group = await onboard_group_admin(client, db_session, org, headers)
    invite = await client.post(
        f"/group-admins/invite-links?group_id={group.id}", json={"expires_in_days": 7}, headers=headers
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

    members = await client.get(f"/group-admins/members?group_id={group.id}", headers=headers)
    assert members.status_code == 200
    assert len(members.json()["data"]["items"]) == 1
    assert members.json()["data"]["items"][0]["name"] == "Traceable Member"
    assert members.json()["data"]["items"][0]["invite_source"] is not None


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

    org_b, _existing_group_b = await create_org_and_group(
        db_session, org_name="Other Uni", org_short_code="OU4", group_name="Other Dept", group_short_code="OD4"
    )
    token_b, _, _ = await _create_invite_link(client, db_session, org=org_b)

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

    verify_token = await find_redis_token("verify_email")
    await client.post(
        "/auth/verify-email", json={"email": "same-group-twice@example.com", "token": verify_token}
    )
    login = await client.post(
        "/auth/login", json={"email": "same-group-twice@example.com", "password": "password123"}
    )
    member_headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}

    # A fresh invite link for the SAME group -- already a member there.
    admin_token = await _register_and_login_group_admin(client, email="rep2@example.com")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    from app.modules.group_admins.models import GroupAdmin

    (
        await db_session.execute(select(User).where(User.email == "rep2@example.com"))
    ).scalar_one()
    user_2 = (await db_session.execute(select(User).where(User.email == "rep2@example.com"))).scalar_one()
    db_session.add(GroupAdmin(user_id=user_2.id, group_id=group.id))
    await db_session.commit()

    another_invite = await client.post(
        f"/group-admins/invite-links?group_id={group.id}", json={"expires_in_days": 7}, headers=admin_headers
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

    verify_token = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"email": "two-groups@example.com", "token": verify_token})
    login = await client.post(
        "/auth/login", json={"email": "two-groups@example.com", "password": "password123"}
    )
    member_headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}

    org_b, _existing_group_b = await create_org_and_group(
        db_session, org_name="Second Uni", org_short_code="SU5", group_name="Second Dept", group_short_code="SD5"
    )
    token_b, _, group_b = await _create_invite_link(
        client, db_session, org=org_b, cohort="200", admin_email="rep-b@example.com"
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

    org, _existing_group = await create_org_and_group(db_session)
    await onboard_group_admin(client, db_session, org, admin_headers)

    other_org, _existing_other_group = await create_org_and_group(
        db_session, org_name="Third Uni", org_short_code="TU6", group_name="Third Dept", group_short_code="TD6"
    )
    token, _, other_group = await _create_invite_link(
        client, db_session, org=other_org, admin_email="rep-c@example.com"
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

    # This member's own group's admin already exists (created by
    # _create_invite_link) -- reuse it to create the purse rather than
    # onboarding an unrelated second admin who wouldn't have access to it.
    admin_token = await _register_and_login_group_admin(client, email="rep3@example.com")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    from app.modules.group_admins.models import GroupAdmin
    from app.modules.auth.models import User as UserModel

    rep3 = (await db_session.execute(select(UserModel).where(UserModel.email == "rep3@example.com"))).scalar_one()
    db_session.add(GroupAdmin(user_id=rep3.id, group_id=group.id))
    await db_session.commit()

    await client.post(
        "/purses",
        json={
            "group_id": str(group.id),
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
