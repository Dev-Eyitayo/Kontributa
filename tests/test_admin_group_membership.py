from sqlalchemy import select

from app.core.auth import create_access_token
from app.modules.audit.models import AuditLog
from app.modules.members.models import Member
from tests.conftest import (
    create_org_and_group,
    create_platform_admin,
    find_redis_token,
    onboard_group_admin,
)


async def _admin_headers(db_session):
    admin = await create_platform_admin(db_session)
    token = create_access_token(admin.id, "group_admin")
    return {"Authorization": f"Bearer {token.token}"}, admin


async def _setup_group_with_member(client, db_session, email="rep@example.com", member_email="member@example.com"):
    org, _existing_group = await create_org_and_group(db_session)
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
    admin_headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
    group = await onboard_group_admin(client, db_session, org, admin_headers)

    invite = await client.post(
        f"/group-admins/invite-links?group_id={group.id}", json={"expires_in_days": 7}, headers=admin_headers
    )
    token = invite.json()["data"]["token"]
    await client.post(
        f"/members/join/{token}",
        json={"email": member_email, "password": "password123", "first_name": "Ada", "last_name": "Lovelace"},
    )
    member_verify_token = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"email": member_email, "token": member_verify_token})

    result = await db_session.execute(select(Member).where(Member.group_id == group.id))
    member = result.scalar_one()
    return org, group, member, admin_headers


async def test_platform_admin_can_edit_group_and_it_is_audit_logged(client, db_session):
    headers, admin_user = await _admin_headers(db_session)
    org, group, _member, _admin_headers_group = await _setup_group_with_member(
        client, db_session, email="group-edit-rep@example.com", member_email="group-edit-member@example.com"
    )

    resp = await client.patch(
        f"/admin/groups/{group.id}",
        json={"name": "Renamed Department", "cohort": "2027"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["name"] == "Renamed Department"
    assert body["cohort"] == "2027"

    entries = (
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.entity_type == "group", AuditLog.entity_id == group.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(entries) == 1
    entry = entries[0]
    assert entry.action == "group_edited_by_platform_admin"
    assert entry.actor_id == admin_user.id
    assert entry.before_state["name"] != "Renamed Department"
    assert entry.after_state["name"] == "Renamed Department"


async def test_group_edit_cohort_change_cascades_to_members(client, db_session):
    """A platform admin editing a group's cohort immediately moves every
    active member onto it too -- same cascade the group admin's own
    PATCH /groups/{id} does (see GroupAdminService.update_group)."""
    headers, _admin_user = await _admin_headers(db_session)
    org, group, member, _ = await _setup_group_with_member(
        client, db_session, email="cohort-rep@example.com", member_email="cohort-member@example.com"
    )

    resp = await client.patch(f"/admin/groups/{group.id}", json={"cohort": "2099"}, headers=headers)
    assert resp.status_code == 200

    await db_session.refresh(member)
    assert member.cohort == "2099"


async def test_platform_admin_can_list_group_members(client, db_session):
    headers, _ = await _admin_headers(db_session)
    org, group, member, _ = await _setup_group_with_member(
        client, db_session, email="list-rep@example.com", member_email="list-member@example.com"
    )

    resp = await client.get(f"/admin/groups/{group.id}/members", headers=headers)
    assert resp.status_code == 200, resp.text
    items = resp.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["id"] == str(member.id)
    assert items[0]["name"] == "Ada Lovelace"


async def test_platform_admin_can_edit_member_profile_and_it_is_audit_logged(client, db_session):
    headers, admin_user = await _admin_headers(db_session)
    org, group, member, _ = await _setup_group_with_member(
        client, db_session, email="member-edit-rep@example.com", member_email="member-edit-member@example.com"
    )

    resp = await client.patch(
        f"/admin/members/{member.id}",
        json={"first_name": "Grace", "last_name": "Hopper", "member_id_number": "22/CS/1234"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["name"] == "Grace Hopper"
    assert resp.json()["data"]["member_id_number"] == "22/CS/1234"

    entries = (
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.entity_type == "member", AuditLog.entity_id == member.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(entries) == 1
    assert entries[0].action == "member_profile_edited_by_platform_admin"
    assert entries[0].actor_id == admin_user.id
    assert entries[0].before_state["first_name"] == "Ada"
    assert entries[0].after_state["first_name"] == "Grace"


async def test_platform_admin_can_remove_member_and_it_is_audit_logged(client, db_session):
    headers, admin_user = await _admin_headers(db_session)
    org, group, member, _ = await _setup_group_with_member(
        client, db_session, email="remove-rep@example.com", member_email="remove-member@example.com"
    )

    resp = await client.delete(f"/admin/members/{member.id}", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["removed"] is True

    await db_session.refresh(member)
    assert member.removed_at is not None

    listing = await client.get(f"/admin/groups/{group.id}/members", headers=headers)
    assert listing.json()["data"]["items"] == []

    entries = (
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.entity_type == "member", AuditLog.entity_id == member.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(entries) == 1
    assert entries[0].action == "member_removed_by_platform_admin"
    assert entries[0].actor_id == admin_user.id


async def test_removed_member_operations_return_404(client, db_session):
    headers, _ = await _admin_headers(db_session)
    org, group, member, _ = await _setup_group_with_member(
        client, db_session, email="gone-rep@example.com", member_email="gone-member@example.com"
    )
    await client.delete(f"/admin/members/{member.id}", headers=headers)

    resp = await client.patch(f"/admin/members/{member.id}", json={"first_name": "Nope"}, headers=headers)
    assert resp.status_code == 404

    second_remove = await client.delete(f"/admin/members/{member.id}", headers=headers)
    assert second_remove.status_code == 404


async def test_group_and_member_admin_endpoints_require_platform_admin(client, db_session):
    org, group, member, group_admin_headers = await _setup_group_with_member(
        client, db_session, email="noauth-rep@example.com", member_email="noauth-member@example.com"
    )

    unauth_patch = await client.patch(f"/admin/groups/{group.id}", json={"name": "Nope"})
    assert unauth_patch.status_code == 401

    # A regular group admin (not platform admin) must not be able to reach these either.
    forbidden = await client.patch(f"/admin/groups/{group.id}", json={"name": "Nope"}, headers=group_admin_headers)
    assert forbidden.status_code == 403

    forbidden_member = await client.patch(
        f"/admin/members/{member.id}", json={"first_name": "Nope"}, headers=group_admin_headers
    )
    assert forbidden_member.status_code == 403
