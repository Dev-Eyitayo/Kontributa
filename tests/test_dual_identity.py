"""An account can now hold an active GroupAdmin row for some groups and an
active Member row for others at the same time. These tests cover: (1) both
cross-identity acquisition paths actually work end to end, (2) GET /auth/me
reports the resulting has_admin_identity/has_member_identity flags
correctly, and (3) the identity-based gating change in
get_current_group_admin_user/get_current_member_user doesn't regress the
existing "not onboarded yet, still gets an empty list" behavior."""

from tests.conftest import create_org_and_group, find_redis_token, onboard_group_admin


async def _register_verify_login(client, email: str, role: str, password: str = "password123") -> dict:
    await client.post(
        "/auth/register",
        json={
            "email": email,
            "password": password,
            "first_name": "Test",
            "last_name": "User",
            "role": role,
        },
    )
    verify_token = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"email": email, "token": verify_token})
    login = await client.post("/auth/login", json={"email": email, "password": password})
    token = login.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def test_group_admin_can_join_another_group_as_member(client, db_session):
    org, _existing_group = await create_org_and_group(db_session)
    admin_headers = await _register_verify_login(client, "dual-admin@example.com", "group_admin")
    await onboard_group_admin(client, db_session, org, admin_headers, group_name="Admin's Own Group")

    # A second, unrelated group the same account wants to join as a Member.
    other_admin_headers = await _register_verify_login(client, "other-admin@example.com", "group_admin")
    other_group = await onboard_group_admin(client, db_session, org, other_admin_headers, group_name="Other Group")

    invite = await client.post(
        f"/group-admins/invite-links?group_id={other_group.id}",
        json={"expires_in_days": 7},
        headers=other_admin_headers,
    )
    token = invite.json()["data"]["token"]

    join = await client.post(f"/members/join-additional/{token}", json={}, headers=admin_headers)
    assert join.status_code == 201, join.text

    # The account's JWT role claim is still "group_admin" -- a static
    # value from registration -- yet Member-only endpoints must now be
    # reachable, since get_current_member_user checks the live Member row,
    # not that claim.
    me = await client.get("/members/me", headers=admin_headers)
    assert me.status_code == 200, me.text

    groups = await client.get("/members/me/groups", headers=admin_headers)
    assert groups.status_code == 200
    assert len(groups.json()["data"]) == 1


async def test_member_can_onboard_as_group_admin(client, db_session):
    org, _existing_group = await create_org_and_group(db_session)
    admin_headers = await _register_verify_login(client, "seed-admin@example.com", "group_admin")
    seed_group = await onboard_group_admin(client, db_session, org, admin_headers, group_name="Seed Group")

    invite = await client.post(
        f"/group-admins/invite-links?group_id={seed_group.id}",
        json={"expires_in_days": 7},
        headers=admin_headers,
    )
    token = invite.json()["data"]["token"]

    join = await client.post(
        f"/members/join/{token}",
        json={
            "email": "dual-member@example.com",
            "password": "password123",
            "first_name": "Dual",
            "last_name": "Identity",
        },
    )
    assert join.status_code == 201, join.text
    verify_token = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"email": "dual-member@example.com", "token": verify_token})
    login = await client.post(
        "/auth/login", json={"email": "dual-member@example.com", "password": "password123"}
    )
    member_headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}

    # Was blocked before this feature: onboard() used to require
    # get_current_group_admin_user (role == "group_admin" on the JWT).
    # This Member account's JWT role is "member" -- onboarding must still
    # succeed, since it's the acquisition path for the GroupAdmin identity
    # itself.
    onboard = await client.post(
        "/group-admins/onboard",
        json={"new_group_name": "The Member's Own Group"},
        headers=member_headers,
    )
    assert onboard.status_code == 201, onboard.text
    group_id = onboard.json()["data"]["group_id"]

    me = await client.get(f"/group-admins/me?group_id={group_id}", headers=member_headers)
    assert me.status_code == 200, me.text

    my_groups = await client.get("/group-admins/me/groups", headers=member_headers)
    assert my_groups.status_code == 200
    assert len(my_groups.json()["data"]) == 1


async def test_auth_me_reports_dual_identity_flags(client, db_session):
    org, _existing_group = await create_org_and_group(db_session)
    admin_headers = await _register_verify_login(client, "flags-admin@example.com", "group_admin")
    flags_group = await onboard_group_admin(client, db_session, org, admin_headers, group_name="Flags Group")

    before = await client.get("/auth/me", headers=admin_headers)
    assert before.json()["data"]["has_admin_identity"] is True
    assert before.json()["data"]["has_member_identity"] is False

    invite = await client.post(
        f"/group-admins/invite-links?group_id={flags_group.id}",
        json={"expires_in_days": 7},
        headers=admin_headers,
    )
    token = invite.json()["data"]["token"]
    join = await client.post(f"/members/join-additional/{token}", json={}, headers=admin_headers)
    assert join.status_code == 201, join.text

    after = await client.get("/auth/me", headers=admin_headers)
    assert after.json()["data"]["has_admin_identity"] is True
    assert after.json()["data"]["has_member_identity"] is True


async def test_auth_me_groups_combines_admin_and_member_rows_with_role_tags(client, db_session):
    org, _existing_group = await create_org_and_group(db_session)
    admin_headers = await _register_verify_login(client, "combined-admin@example.com", "group_admin")
    owned_group = await onboard_group_admin(client, db_session, org, admin_headers, group_name="Owned Group")

    only_admin = await client.get("/auth/me/groups", headers=admin_headers)
    assert only_admin.status_code == 200
    assert only_admin.json()["data"] == [
        {"group_id": str(owned_group.id), "group_name": "Owned Group", "short_code": owned_group.short_code, "role": "admin"}
    ]

    other_admin_headers = await _register_verify_login(client, "combined-other-admin@example.com", "group_admin")
    joined_group = await onboard_group_admin(
        client, db_session, org, other_admin_headers, group_name="Joined Group"
    )
    invite = await client.post(
        f"/group-admins/invite-links?group_id={joined_group.id}",
        json={"expires_in_days": 7},
        headers=other_admin_headers,
    )
    token = invite.json()["data"]["token"]
    join = await client.post(f"/members/join-additional/{token}", json={}, headers=admin_headers)
    assert join.status_code == 201, join.text

    combined = await client.get("/auth/me/groups", headers=admin_headers)
    assert combined.status_code == 200
    entries = combined.json()["data"]
    assert len(entries) == 2
    roles_by_group = {e["group_id"]: e["role"] for e in entries}
    assert roles_by_group[str(owned_group.id)] == "admin"
    assert roles_by_group[str(joined_group.id)] == "member"


async def test_group_admin_role_before_onboarding_still_gets_empty_groups_list(client):
    """Regression guard: get_current_group_admin_user became identity-
    based (an active GroupAdmin row), not role-based -- a freshly
    registered group_admin-role account who hasn't onboarded yet must
    still get 200 with an empty list here (this is exactly what
    GroupProvider on the frontend uses to decide whether to redirect to
    /onboarding), not a 403."""
    headers = await _register_verify_login(client, "not-onboarded@example.com", "group_admin")

    resp = await client.get("/group-admins/me/groups", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"] == []


async def test_member_role_before_joining_still_gets_empty_groups_list(client):
    headers = await _register_verify_login(client, "not-joined@example.com", "member")

    resp = await client.get("/members/me/groups", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"] == []


async def test_member_without_group_admin_identity_still_forbidden_from_group_admin_endpoints(client, db_session):
    org, _existing_group = await create_org_and_group(db_session)
    admin_headers = await _register_verify_login(client, "plain-admin@example.com", "group_admin")
    group = await onboard_group_admin(client, db_session, org, admin_headers, group_name="Plain Group")

    invite = await client.post(
        f"/group-admins/invite-links?group_id={group.id}", json={"expires_in_days": 7}, headers=admin_headers
    )
    token = invite.json()["data"]["token"]
    join = await client.post(
        f"/members/join/{token}",
        json={
            "email": "plain-member@example.com",
            "password": "password123",
            "first_name": "Plain",
            "last_name": "Member",
        },
    )
    assert join.status_code == 201
    verify_token = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"email": "plain-member@example.com", "token": verify_token})
    login = await client.post(
        "/auth/login", json={"email": "plain-member@example.com", "password": "password123"}
    )
    member_headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}

    resp = await client.get(f"/group-admins/invite-links?group_id={group.id}", headers=member_headers)
    assert resp.status_code == 403
