import pytest
from datetime import datetime, timedelta, timezone
from tests.conftest import create_org_and_group, find_redis_token, onboard_group_admin


async def _register_and_login_member(client, email="member-creator@example.com"):
    await client.post(
        "/auth/register",
        json={
            "email": email,
            "password": "password123",
            "first_name": "Tayo",
            "last_name": "Member",
            "role": "member",
        },
    )
    verify_token = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"email": email, "token": verify_token})
    login = await client.post("/auth/login", json={"email": email, "password": "password123"})
    return login.json()["data"]["access_token"]


def _future_deadline(days=7) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


@pytest.mark.asyncio
async def test_member_role_account_creating_group_can_list_and_query_admin_purses(client, db_session):
    """Verifies that a user registered with base role 'member' who onboards a group
    is correctly recognized as a GroupAdmin when fetching GET /purses?group_id=<id>
    and querying per-purse flagged contributions without receiving 403 Forbidden errors.
    """
    token = await _register_and_login_member(client, "member-admin-test@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    org, _existing_group = await create_org_and_group(db_session)
    group = await onboard_group_admin(client, db_session, org, headers, group_name="Member Created Group")

    # Create a purse as GroupAdmin
    purse_res = await client.post(
        "/purses",
        json={
            "group_id": str(group.id),
            "title": "Community Fund",
            "amount": "5000.00",
            "deadline": _future_deadline(),
            "enroll_mode": "auto_enroll",
        },
        headers=headers,
    )
    assert purse_res.status_code == 201
    purse_id = purse_res.json()["data"]["id"]

    # List purses as GroupAdmin using GET /purses?group_id=<id>
    list_res = await client.get(f"/purses?group_id={group.id}", headers=headers)
    assert list_res.status_code == 200, list_res.text
    body = list_res.json()["data"]
    assert body["total"] == 1
    assert body["items"][0]["id"] == purse_id
    assert "paid_count" in body["items"][0]  # Proves admin shape returned, not member shape

    # Fetch flagged contributions for this purse (the call that previously threw 403)
    flagged_res = await client.get(f"/purses/{purse_id}/contributions?status=flagged_for_review", headers=headers)
    assert flagged_res.status_code == 200, flagged_res.text
    assert "items" in flagged_res.json()["data"]
