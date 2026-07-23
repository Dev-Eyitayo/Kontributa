from app.core.auth import create_access_token
from tests.conftest import create_org_and_group, create_platform_admin


async def _admin_headers(db_session):
    admin = await create_platform_admin(db_session)
    token = create_access_token(admin.id, "group_admin")
    return {"Authorization": f"Bearer {token.token}"}


async def test_list_organizations_empty_success_envelope(client):
    resp = await client.get("/organizations")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"] == []


async def test_admin_create_organization_requires_admin_error_envelope(client, db_session):
    resp = await client.post(
        "/admin/organizations",
        json={"name": "Lead City University", "short_code": "LCU", "org_type": "school"},
    )
    assert resp.status_code == 401
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "missing_token"


async def test_admin_create_organization_success(client, db_session):
    headers = await _admin_headers(db_session)
    resp = await client.post(
        "/admin/organizations",
        json={"name": "Lead City University", "short_code": "LCU", "org_type": "school"},
        headers=headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["org_type"] == "school"
    assert body["data"]["active"] is True


async def test_duplicate_short_code_conflict(client, db_session):
    headers = await _admin_headers(db_session)
    payload = {"name": "Lead City University", "short_code": "LCU", "org_type": "school"}
    first = await client.post("/admin/organizations", json=payload, headers=headers)
    assert first.status_code == 201

    second = await client.post("/admin/organizations", json=payload, headers=headers)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "duplicate_short_code"


async def test_create_group_and_list_groups(client, db_session):
    headers = await _admin_headers(db_session)
    org, _ = await create_org_and_group(db_session)

    create_group = await client.post(
        "/admin/groups",
        json={"organization_id": str(org.id), "name": "Mechanical Engineering", "short_code": "MEE"},
        headers=headers,
    )
    assert create_group.status_code == 201

    listing = await client.get(f"/organizations/{org.id}/groups")
    assert listing.status_code == 200
    names = [g["name"] for g in listing.json()["data"]]
    assert "Computer Science" in names
    assert "Mechanical Engineering" in names


async def test_create_group_under_unknown_organization_returns_404(client, db_session):
    import uuid

    headers = await _admin_headers(db_session)
    resp = await client.post(
        "/admin/groups",
        json={"organization_id": str(uuid.uuid4()), "name": "Ghost Dept", "short_code": "GHOST"},
        headers=headers,
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_admin_list_organizations_requires_admin_error_envelope(client, db_session):
    org, _ = await create_org_and_group(db_session)
    resp = await client.get("/admin/organizations")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "missing_token"


async def test_admin_list_organizations_includes_inactive_and_full_shape(client, db_session):
    headers = await _admin_headers(db_session)
    org, _ = await create_org_and_group(db_session)

    deactivate = await client.patch(
        f"/admin/organizations/{org.id}", json={"active": False}, headers=headers
    )
    assert deactivate.status_code == 200

    public_listing = await client.get("/organizations")
    assert org.name not in [o["name"] for o in public_listing.json()["data"]]

    admin_listing = await client.get("/admin/organizations", headers=headers)
    assert admin_listing.status_code == 200
    rows = admin_listing.json()["data"]
    row = next(r for r in rows if r["name"] == org.name)
    assert row["active"] is False
    assert row["org_type"] == "school"
    assert "member_id_format" in row


async def test_patch_organization_can_configure_member_id_format(client, db_session):
    headers = await _admin_headers(db_session)
    org, _ = await create_org_and_group(db_session)

    resp = await client.patch(
        f"/admin/organizations/{org.id}",
        json={"member_id_format": r"^\d{2}/[A-Z]{2}/\d{4}$"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["member_id_format"] == r"^\d{2}/[A-Z]{2}/\d{4}$"
