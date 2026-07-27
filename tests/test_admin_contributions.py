"""An admin contributing to their own group's purse is just a regular
Contribution row -- no separate table, no separate UI section, no
separate stats. These tests cover purse-creation eager-generation for
every active GroupAdmin, the self-service generate-invoice permission
check, and that the admin's row is included in the same transparency
views and summary figures as any member's."""
from uuid import uuid4

from sqlalchemy import select

from app.modules.auth.models import User
from app.modules.group_admins.models import GroupAdmin
from tests.conftest import create_org_and_group, find_redis_token, onboard_group_admin
from tests.test_purses import _invite_and_join_member, _purse_payload


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


async def _setup_group_with_admin(client, db_session):
    org, _existing_group = await create_org_and_group(db_session)
    admin_token = await _register_and_login_group_admin(client)
    headers = {"Authorization": f"Bearer {admin_token}"}
    group = await onboard_group_admin(client, db_session, org, headers)
    return org, group, headers


async def _add_co_admin(db_session, group, user_id) -> GroupAdmin:
    """No API path creates a second GroupAdmin for an already-existing
    group (co-admin invites aren't a feature yet) -- inserted directly,
    same as this repo's other tests do for states the API can't reach."""
    co_admin = GroupAdmin(id=uuid4(), user_id=user_id, group_id=group.id, is_active_admin=True)
    db_session.add(co_admin)
    await db_session.commit()
    await db_session.refresh(co_admin)
    return co_admin


def _find_row(items, owner_type, name=None):
    for item in items:
        if item["owner_type"] == owner_type and (name is None or item["name"] == name):
            return item
    return None


async def test_purse_creation_creates_contribution_row_for_active_group_admin(client, db_session):
    org, group, headers = await _setup_group_with_admin(client, db_session)

    create = await client.post("/purses", json=_purse_payload(group.id), headers=headers)
    assert create.status_code == 201, create.text
    purse_id = create.json()["data"]["id"]

    listing = await client.get(f"/purses/{purse_id}/contributions", headers=headers)
    assert listing.status_code == 200, listing.text
    items = listing.json()["data"]["items"]

    admin_row = _find_row(items, "admin")
    assert admin_row is not None, f"no admin-owned row in {items}"
    assert admin_row["member_id"] is None
    assert admin_row["group_admin_id"] is not None
    assert admin_row["member_id_number"] is None
    assert admin_row["is_mine"] is True
    assert admin_row["status"] == "pending"
    assert admin_row["name"] == "Tayo Rep"


async def test_two_co_admins_each_get_their_own_contribution_row(client, db_session):
    org, group, headers = await _setup_group_with_admin(client, db_session)

    co_admin_email = "co-admin@example.com"
    await client.post(
        "/auth/register",
        json={
            "email": co_admin_email,
            "password": "password123",
            "first_name": "Co",
            "last_name": "Admin",
            "role": "group_admin",
        },
    )
    verify_token = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"email": co_admin_email, "token": verify_token})
    await client.post("/auth/login", json={"email": co_admin_email, "password": "password123"})

    co_admin_user = (
        (await db_session.execute(select(User).where(User.email == co_admin_email)))
    ).scalar_one()
    await _add_co_admin(db_session, group, co_admin_user.id)

    create = await client.post("/purses", json=_purse_payload(group.id), headers=headers)
    assert create.status_code == 201, create.text
    purse_id = create.json()["data"]["id"]

    listing = await client.get(f"/purses/{purse_id}/contributions", headers=headers)
    items = listing.json()["data"]["items"]
    admin_rows = [i for i in items if i["owner_type"] == "admin"]
    assert len(admin_rows) == 2
    names = {row["name"] for row in admin_rows}
    assert names == {"Tayo Rep", "Co Admin"}
    # Independently pending/paid -- distinct rows, distinct ids.
    assert admin_rows[0]["id"] != admin_rows[1]["id"]


async def test_admin_can_generate_invoice_for_their_own_contribution(client, db_session):
    org, group, headers = await _setup_group_with_admin(client, db_session)

    create = await client.post("/purses", json=_purse_payload(group.id), headers=headers)
    purse_id = create.json()["data"]["id"]

    listing = await client.get(f"/purses/{purse_id}/contributions", headers=headers)
    admin_row = _find_row(listing.json()["data"]["items"], "admin")
    contribution_id = admin_row["id"]

    invoice = await client.post(f"/contributions/{contribution_id}/generate-invoice", headers=headers)
    assert invoice.status_code == 200, invoice.text
    body = invoice.json()["data"]
    assert body["account_number"]
    assert body["bank_name"]
    assert body["amount"] == "500.00"


async def test_admin_cannot_generate_invoice_for_another_admins_contribution(client, db_session):
    org, group, headers = await _setup_group_with_admin(client, db_session)

    co_admin_email = "guard-co-admin@example.com"
    await client.post(
        "/auth/register",
        json={
            "email": co_admin_email,
            "password": "password123",
            "first_name": "Co",
            "last_name": "Admin",
            "role": "group_admin",
        },
    )
    verify_token = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"email": co_admin_email, "token": verify_token})
    login = await client.post("/auth/login", json={"email": co_admin_email, "password": "password123"})
    co_admin_headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}

    co_admin_user = (
        (await db_session.execute(select(User).where(User.email == co_admin_email)))
    ).scalar_one()
    await _add_co_admin(db_session, group, co_admin_user.id)

    create = await client.post("/purses", json=_purse_payload(group.id), headers=headers)
    purse_id = create.json()["data"]["id"]

    listing = await client.get(f"/purses/{purse_id}/contributions", headers=headers)
    items = listing.json()["data"]["items"]
    original_admin_row = _find_row(items, "admin", name="Tayo Rep")

    # The co-admin can see the row (group-scoped transparency access) but
    # must not be able to pay against someone else's own contribution.
    resp = await client.post(
        f"/contributions/{original_admin_row['id']}/generate-invoice", headers=co_admin_headers
    )
    assert resp.status_code == 403


async def test_member_visible_view_includes_admin_row_with_owner_type(client, db_session):
    org, group, headers = await _setup_group_with_admin(client, db_session)
    member_headers = await _invite_and_join_member(client, headers, group.id)

    create = await client.post("/purses", json=_purse_payload(group.id), headers=headers)
    purse_id = create.json()["data"]["id"]

    listing = await client.get(f"/purses/{purse_id}/member-contributions", headers=member_headers)
    assert listing.status_code == 200, listing.text
    items = listing.json()["data"]["items"]
    admin_row = _find_row(items, "admin")
    assert admin_row is not None
    assert admin_row["name"] == "Tayo Rep"
    # Deliberately thin -- no ids, no amounts on this view.
    assert set(admin_row.keys()) == {"name", "owner_type", "status", "display_status"}


async def test_summary_and_stat_counts_include_the_admin_contribution_with_no_special_casing(client, db_session):
    org, group, headers = await _setup_group_with_admin(client, db_session)

    create = await client.post("/purses", json=_purse_payload(group.id), headers=headers)
    purse_id = create.json()["data"]["id"]

    before = await client.get(f"/purses/{purse_id}/summary", headers=headers)
    assert before.json()["data"]["pending_count"] == 1

    listing = await client.get(f"/purses/{purse_id}/contributions", headers=headers)
    admin_row = _find_row(listing.json()["data"]["items"], "admin")

    mark_manual = await client.post(
        f"/contributions/{admin_row['id']}/mark-manual",
        json={"amount_received": "500.00", "note": "cash from the treasurer"},
        headers=headers,
    )
    assert mark_manual.status_code == 200, mark_manual.text

    after = await client.get(f"/purses/{purse_id}/summary", headers=headers)
    summary = after.json()["data"]
    assert summary["pending_count"] == 0
    assert summary["paid_count"] == 1
    assert summary["total_collected"] == "500.00"
