import uuid

from tests.conftest import create_org_and_group, find_redis_token, onboard_group_admin


async def _register_and_login_group_admin(client, email="rep1@example.com"):
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
    token = login.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def test_onboard_success_and_me(client, db_session):
    org, _existing_group = await create_org_and_group(db_session)
    headers = await _register_and_login_group_admin(client)

    onboard = await client.post(
        "/group-admins/onboard",
        json={"organization_id": str(org.id), "new_group_name": "400L Software Engineering", "cohort": "400L"},
        headers=headers,
    )
    assert onboard.status_code == 201
    body = onboard.json()
    assert body["success"] is True
    assert body["data"]["cohort"] == "400L"
    assert body["data"]["is_active_admin"] is True
    assert body["data"]["group_name"] == "400L Software Engineering"
    group_id = body["data"]["group_id"]

    me = await client.get(f"/group-admins/me?group_id={group_id}", headers=headers)
    assert me.status_code == 200
    assert me.json()["data"]["group"]["name"] == "400L Software Engineering"
    assert me.json()["data"]["members_count"] == 0


async def test_onboard_never_grants_control_of_an_existing_group(client, db_session):
    """Part 1's core fix: the old request shape (organization_id + an
    existing group_id) no longer exists at all -- there is no field in
    the request that can even name an existing group, so there is no way
    for a new admin to end up controlling one they didn't create."""
    org, existing_group = await create_org_and_group(db_session)
    headers = await _register_and_login_group_admin(client)

    # The old shape is simply not a field this endpoint accepts anymore --
    # sending it has no effect (extra fields are ignored), it can't smuggle
    # control of the pre-existing group in.
    onboard = await client.post(
        "/group-admins/onboard",
        json={"organization_id": str(org.id), "group_id": str(existing_group.id), "new_group_name": "My Own Group"},
        headers=headers,
    )
    assert onboard.status_code == 201
    assert onboard.json()["data"]["group_id"] != str(existing_group.id)
    assert onboard.json()["data"]["group_name"] == "My Own Group"

    # And the requesting admin has no access to the pre-existing group at all.
    me = await client.get(f"/group-admins/me?group_id={existing_group.id}", headers=headers)
    assert me.status_code == 403


async def test_onboard_with_unknown_organization_404s(client, db_session):
    headers = await _register_and_login_group_admin(client)
    resp = await client.post(
        "/group-admins/onboard",
        json={"organization_id": str(uuid.uuid4()), "new_group_name": "Ghost Org Group"},
        headers=headers,
    )
    assert resp.status_code == 404


async def test_onboard_twice_creates_two_distinct_groups(client, db_session):
    """An admin can now manage more than one group -- calling onboard a
    second time is a real, supported way to create an additional group,
    not a conflict."""
    org, _existing_group = await create_org_and_group(db_session)
    headers = await _register_and_login_group_admin(client)

    first = await onboard_group_admin(client, db_session, org, headers, group_name="First Group")
    second = await onboard_group_admin(client, db_session, org, headers, group_name="Second Group")

    assert first.id != second.id

    groups = await client.get("/group-admins/me/groups", headers=headers)
    assert groups.status_code == 200
    group_ids = {g["id"] for g in groups.json()["data"]}
    assert group_ids == {str(first.id), str(second.id)}


async def test_admin_can_switch_between_groups_and_cannot_access_a_third(client, db_session):
    org, _existing_group = await create_org_and_group(db_session)
    headers = await _register_and_login_group_admin(client)

    group_a = await onboard_group_admin(client, db_session, org, headers, group_name="Group A")
    group_b = await onboard_group_admin(client, db_session, org, headers, group_name="Group B")

    me_a = await client.get(f"/group-admins/me?group_id={group_a.id}", headers=headers)
    assert me_a.status_code == 200
    assert me_a.json()["data"]["group"]["name"] == "Group A"

    me_b = await client.get(f"/group-admins/me?group_id={group_b.id}", headers=headers)
    assert me_b.status_code == 200
    assert me_b.json()["data"]["group"]["name"] == "Group B"

    # A third admin's group -- this admin has no GroupAdmin row for it at all.
    other_headers = await _register_and_login_group_admin(client, email="unrelated-rep@example.com")
    group_c = await onboard_group_admin(client, db_session, org, other_headers, group_name="Group C")

    forbidden = await client.get(f"/group-admins/me?group_id={group_c.id}", headers=headers)
    assert forbidden.status_code == 403


async def test_group_id_scoped_endpoints_reject_a_group_the_admin_does_not_manage(client, db_session):
    org, _existing_group = await create_org_and_group(db_session)
    headers = await _register_and_login_group_admin(client)
    own_group = await onboard_group_admin(client, db_session, org, headers, group_name="My Group")

    other_headers = await _register_and_login_group_admin(client, email="other-rep@example.com")
    other_group = await onboard_group_admin(client, db_session, org, other_headers, group_name="Other Group")

    purses = await client.get(f"/purses?group_id={other_group.id}", headers=headers)
    assert purses.status_code == 403

    members = await client.get(f"/group-admins/members?group_id={other_group.id}", headers=headers)
    assert members.status_code == 403

    create_purse = await client.post(
        "/purses",
        json={
            "group_id": str(other_group.id),
            "title": "Sneaky Fee",
            "amount": "100.00",
            "deadline": "2999-01-01T00:00:00Z",
            "enroll_mode": "snapshot",
        },
        headers=headers,
    )
    assert create_purse.status_code == 403

    # Sanity check: the admin's own group works fine with the same shape of call.
    own_purses = await client.get(f"/purses?group_id={own_group.id}", headers=headers)
    assert own_purses.status_code == 200


async def test_invite_link_lifecycle(client, db_session):
    org, _existing_group = await create_org_and_group(db_session)
    headers = await _register_and_login_group_admin(client)
    group = await onboard_group_admin(client, db_session, org, headers)

    create = await client.post(
        f"/group-admins/invite-links?group_id={group.id}",
        json={"expires_in_days": 7, "max_uses": 5},
        headers=headers,
    )
    assert create.status_code == 201
    invite_id = create.json()["data"]["id"]
    assert create.json()["data"]["token"] in create.json()["data"]["url"]

    listing = await client.get(f"/group-admins/invite-links?group_id={group.id}", headers=headers)
    assert listing.status_code == 200
    assert listing.json()["data"]["total"] == 1
    assert len(listing.json()["data"]["items"]) == 1
    assert listing.json()["data"]["items"][0]["active"] is True

    revoke = await client.delete(f"/group-admins/invite-links/{invite_id}?group_id={group.id}", headers=headers)
    assert revoke.status_code == 200
    assert revoke.json()["data"]["revoked"] is True

    listing_after = await client.get(f"/group-admins/invite-links?group_id={group.id}", headers=headers)
    assert listing_after.json()["data"]["items"][0]["active"] is False


async def test_revoke_another_admins_invite_is_forbidden(client, db_session):
    org, _existing_group = await create_org_and_group(db_session)
    headers_a = await _register_and_login_group_admin(client, email="repA@example.com")
    headers_b = await _register_and_login_group_admin(client, email="repB@example.com")

    group_a = await onboard_group_admin(client, db_session, org, headers_a, group_name="Group A")
    group_b = await onboard_group_admin(client, db_session, org, headers_b, group_name="Group B")

    create = await client.post(
        f"/group-admins/invite-links?group_id={group_a.id}", json={"expires_in_days": 7}, headers=headers_a
    )
    invite_id = create.json()["data"]["id"]

    forbidden = await client.delete(f"/group-admins/invite-links/{invite_id}?group_id={group_b.id}", headers=headers_b)
    assert forbidden.status_code == 403


async def test_members_endpoint_requires_group_admin_role(client, db_session):
    resp = await client.get(
        f"/group-admins/members?group_id={uuid.uuid4()}", headers={"Authorization": f"Bearer {uuid.uuid4()}"}
    )
    assert resp.status_code == 401


async def test_invite_links_pagination(client, db_session):
    org, _existing_group = await create_org_and_group(db_session)
    headers = await _register_and_login_group_admin(client)
    group = await onboard_group_admin(client, db_session, org, headers)

    for _ in range(3):
        await client.post(f"/group-admins/invite-links?group_id={group.id}", json={"expires_in_days": 7}, headers=headers)

    first_page = await client.get(f"/group-admins/invite-links?group_id={group.id}&limit=2&offset=0", headers=headers)
    assert first_page.status_code == 200
    body = first_page.json()["data"]
    assert body["total"] == 3
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert len(body["items"]) == 2

    second_page = await client.get(f"/group-admins/invite-links?group_id={group.id}&limit=2&offset=2", headers=headers)
    body_2 = second_page.json()["data"]
    assert body_2["total"] == 3
    assert len(body_2["items"]) == 1

    default_page = await client.get(f"/group-admins/invite-links?group_id={group.id}", headers=headers)
    default_body = default_page.json()["data"]
    assert default_body["limit"] == 20
    assert len(default_body["items"]) == 3

    over_limit = await client.get(f"/group-admins/invite-links?group_id={group.id}&limit=500", headers=headers)
    assert over_limit.status_code == 422


async def test_members_pagination(client, db_session):
    org, _existing_group = await create_org_and_group(db_session)
    headers = await _register_and_login_group_admin(client)
    group = await onboard_group_admin(client, db_session, org, headers)

    invite = await client.post(f"/group-admins/invite-links?group_id={group.id}", json={"expires_in_days": 7}, headers=headers)
    token = invite.json()["data"]["token"]

    for n in range(3):
        await client.post(
            f"/members/join/{token}",
            json={
                "email": f"page-member-{n}@example.com",
                "password": "password123",
                "first_name": "Member",
                "last_name": str(n),
            },
        )

    first_page = await client.get(f"/group-admins/members?group_id={group.id}&limit=2&offset=0", headers=headers)
    assert first_page.status_code == 200
    body = first_page.json()["data"]
    assert body["total"] == 3
    assert len(body["items"]) == 2

    second_page = await client.get(f"/group-admins/members?group_id={group.id}&limit=2&offset=2", headers=headers)
    body_2 = second_page.json()["data"]
    assert len(body_2["items"]) == 1
