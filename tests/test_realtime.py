import json

from sqlalchemy import select

from app.modules.contributions.models import Contribution
from tests.conftest import _state, create_org_and_group, find_redis_token, onboard_group_admin
from tests.test_contributions_and_webhooks import _generate_invoice, _setup_purse_with_member


async def test_realtime_token_scoped_to_own_contribution(client, db_session):
    ctx = await _setup_purse_with_member(client, db_session)
    await _generate_invoice(client, ctx["member_headers"], ctx["contribution_id"])

    resp = await client.get(
        f"/realtime/token?contribution_id={ctx['contribution_id']}", headers=ctx["member_headers"]
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    assert data["keyName"] == "fake-app.fake-key"
    capability = json.loads(data["capability"])
    assert capability == {f"contribution:{ctx['contribution_id']}": ["subscribe"]}
    assert data["clientId"]
    assert "mac" in data and data["mac"]


async def test_realtime_token_rejected_for_another_members_contribution(client, db_session):
    ctx = await _setup_purse_with_member(client, db_session)

    org, _existing_group = await create_org_and_group(
        db_session, org_name="Other University", org_short_code="OTH", group_name="Other Group", group_short_code="OTG"
    )
    await client.post(
        "/auth/register",
        json={
            "email": "other-rep@example.com",
            "password": "password123",
            "first_name": "Other",
            "last_name": "Rep",
            "role": "group_admin",
        },
    )
    verify_token = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"email": "other-rep@example.com", "token": verify_token})
    login = await client.post("/auth/login", json={"email": "other-rep@example.com", "password": "password123"})
    other_headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
    other_group = await onboard_group_admin(client, db_session, org, other_headers)

    invite = await client.post(
        f"/group-admins/invite-links?group_id={other_group.id}",
        json={"expires_in_days": 7},
        headers=other_headers,
    )
    token = invite.json()["data"]["token"]
    await client.post(
        f"/members/join/{token}",
        json={"email": "other-member@example.com", "password": "password123", "first_name": "Bob", "last_name": "Two"},
    )
    other_member_verify = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"email": "other-member@example.com", "token": other_member_verify})
    other_member_login = await client.post(
        "/auth/login", json={"email": "other-member@example.com", "password": "password123"}
    )
    other_member_headers = {"Authorization": f"Bearer {other_member_login.json()['data']['access_token']}"}

    resp = await client.get(
        f"/realtime/token?contribution_id={ctx['contribution_id']}", headers=other_member_headers
    )
    assert resp.status_code == 403


async def test_realtime_token_rejected_for_group_admin_role(client, db_session):
    ctx = await _setup_purse_with_member(client, db_session)

    resp = await client.get(
        f"/realtime/token?contribution_id={ctx['contribution_id']}", headers=ctx["admin_headers"]
    )
    assert resp.status_code == 403


async def test_webhook_paid_publishes_realtime_status_change(client, db_session):
    from tests.test_contributions_and_webhooks import _sign, _webhook_body

    ctx = await _setup_purse_with_member(client, db_session, amount="2500.00")
    await _generate_invoice(client, ctx["member_headers"], ctx["contribution_id"])

    result = await db_session.execute(select(Contribution).where(Contribution.id == ctx["contribution_id"]))
    contribution = result.scalar_one()

    _state["realtime"].published.clear()
    body = _webhook_body(contribution.invoice_id, "2500.00")
    resp = await client.post("/webhooks/monnify", content=body, headers={"monnify-signature": _sign(body)})
    assert resp.status_code == 202

    published = _state["realtime"].published
    assert len(published) == 1
    assert published[0]["channel"] == f"contribution:{ctx['contribution_id']}"
    assert published[0]["name"] == "status_change"
    assert published[0]["data"]["status"] == "paid"
    assert published[0]["data"]["amount_received"] == "2500.00"
