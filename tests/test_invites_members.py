import uuid

from tests.conftest import create_org_and_group


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
    login = await client.post("/auth/login", json={"email": email, "password": "password123"})
    return login.json()["data"]["access_token"]


async def _create_invite_link(client, db_session, org=None, group=None, cohort=None, member_id_format=None):
    if org is None or group is None:
        org, group = await create_org_and_group(db_session, member_id_format=member_id_format)
    admin_token = await _register_and_login_group_admin(client)
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
