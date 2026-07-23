import uuid

from tests.conftest import create_org_and_group


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
    login = await client.post("/auth/login", json={"email": email, "password": "password123"})
    token = login.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def test_onboard_success_and_me(client, db_session):
    org, group = await create_org_and_group(db_session)
    headers = await _register_and_login_group_admin(client)

    onboard = await client.post(
        "/group-admins/onboard",
        json={"organization_id": str(org.id), "group_id": str(group.id), "cohort": "400L"},
        headers=headers,
    )
    assert onboard.status_code == 201
    body = onboard.json()
    assert body["success"] is True
    assert body["data"]["cohort"] == "400L"
    assert body["data"]["is_active_admin"] is True

    me = await client.get("/group-admins/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["data"]["group"]["short_code"] == "CSC"
    assert me.json()["data"]["members_count"] == 0


async def test_onboard_group_organization_mismatch_error_envelope(client, db_session):
    org, group = await create_org_and_group(db_session)
    other_org, _ = await create_org_and_group(
        db_session, org_name="Other Uni", org_short_code="OU", group_name="Other Dept", group_short_code="OD"
    )
    headers = await _register_and_login_group_admin(client)

    resp = await client.post(
        "/group-admins/onboard",
        json={"organization_id": str(other_org.id), "group_id": str(group.id)},
        headers=headers,
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "business_rule_violation"


async def test_onboard_twice_conflicts(client, db_session):
    org, group = await create_org_and_group(db_session)
    headers = await _register_and_login_group_admin(client)

    payload = {"organization_id": str(org.id), "group_id": str(group.id)}
    first = await client.post("/group-admins/onboard", json=payload, headers=headers)
    assert first.status_code == 201

    second = await client.post("/group-admins/onboard", json=payload, headers=headers)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "already_onboarded"


async def test_invite_link_lifecycle(client, db_session):
    org, group = await create_org_and_group(db_session)
    headers = await _register_and_login_group_admin(client)
    await client.post(
        "/group-admins/onboard",
        json={"organization_id": str(org.id), "group_id": str(group.id)},
        headers=headers,
    )

    create = await client.post(
        "/group-admins/invite-links",
        json={"expires_in_days": 7, "max_uses": 5},
        headers=headers,
    )
    assert create.status_code == 201
    invite_id = create.json()["data"]["id"]
    assert create.json()["data"]["token"] in create.json()["data"]["url"]

    listing = await client.get("/group-admins/invite-links", headers=headers)
    assert listing.status_code == 200
    assert listing.json()["data"]["total"] == 1
    assert len(listing.json()["data"]["items"]) == 1
    assert listing.json()["data"]["items"][0]["active"] is True

    revoke = await client.delete(f"/group-admins/invite-links/{invite_id}", headers=headers)
    assert revoke.status_code == 200
    assert revoke.json()["data"]["revoked"] is True

    listing_after = await client.get("/group-admins/invite-links", headers=headers)
    assert listing_after.json()["data"]["items"][0]["active"] is False


async def test_revoke_another_admins_invite_is_forbidden(client, db_session):
    org, group = await create_org_and_group(db_session)
    headers_a = await _register_and_login_group_admin(client, email="repA@example.com")
    headers_b = await _register_and_login_group_admin(client, email="repB@example.com")

    await client.post(
        "/group-admins/onboard",
        json={"organization_id": str(org.id), "group_id": str(group.id)},
        headers=headers_a,
    )
    await client.post(
        "/group-admins/onboard",
        json={"organization_id": str(org.id), "group_id": str(group.id)},
        headers=headers_b,
    )

    create = await client.post(
        "/group-admins/invite-links", json={"expires_in_days": 7}, headers=headers_a
    )
    invite_id = create.json()["data"]["id"]

    forbidden = await client.delete(f"/group-admins/invite-links/{invite_id}", headers=headers_b)
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "forbidden"


async def test_members_endpoint_requires_group_admin_role(client, db_session):
    resp = await client.get("/group-admins/members", headers={"Authorization": f"Bearer {uuid.uuid4()}"})
    assert resp.status_code == 401


async def test_invite_links_pagination(client, db_session):
    org, group = await create_org_and_group(db_session)
    headers = await _register_and_login_group_admin(client)
    await client.post(
        "/group-admins/onboard",
        json={"organization_id": str(org.id), "group_id": str(group.id)},
        headers=headers,
    )

    for _ in range(3):
        await client.post("/group-admins/invite-links", json={"expires_in_days": 7}, headers=headers)

    first_page = await client.get("/group-admins/invite-links?limit=2&offset=0", headers=headers)
    assert first_page.status_code == 200
    body = first_page.json()["data"]
    assert body["total"] == 3
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert len(body["items"]) == 2

    second_page = await client.get("/group-admins/invite-links?limit=2&offset=2", headers=headers)
    body_2 = second_page.json()["data"]
    assert body_2["total"] == 3
    assert len(body_2["items"]) == 1

    default_page = await client.get("/group-admins/invite-links", headers=headers)
    default_body = default_page.json()["data"]
    assert default_body["limit"] == 20
    assert len(default_body["items"]) == 3

    over_limit = await client.get("/group-admins/invite-links?limit=500", headers=headers)
    assert over_limit.status_code == 422


async def test_members_pagination(client, db_session):
    org, group = await create_org_and_group(db_session)
    headers = await _register_and_login_group_admin(client)
    await client.post(
        "/group-admins/onboard",
        json={"organization_id": str(org.id), "group_id": str(group.id)},
        headers=headers,
    )
    invite = await client.post("/group-admins/invite-links", json={"expires_in_days": 7}, headers=headers)
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

    first_page = await client.get("/group-admins/members?limit=2&offset=0", headers=headers)
    assert first_page.status_code == 200
    body = first_page.json()["data"]
    assert body["total"] == 3
    assert len(body["items"]) == 2

    second_page = await client.get("/group-admins/members?limit=2&offset=2", headers=headers)
    body_2 = second_page.json()["data"]
    assert len(body_2["items"]) == 1
