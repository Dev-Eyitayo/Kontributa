from app.core.auth import create_access_token
from tests.conftest import create_platform_admin


async def _admin_headers(db_session):
    admin = await create_platform_admin(db_session)
    token = create_access_token(admin.id, "group_admin")
    return {"Authorization": f"Bearer {token.token}"}


async def test_get_settings_returns_the_singleton_row(client, db_session):
    # conftest's autouse default_platform_settings fixture seeds
    # custodian_mode_enabled=True as this suite's baseline (see its
    # docstring) -- a fresh, unseeded deployment defaults to False instead
    # (see PlatformSettings.custodian_mode_enabled), which is what the
    # kill-switch tests in test_settlement_and_payouts.py explicitly flip
    # back off to exercise.
    headers = await _admin_headers(db_session)
    resp = await client.get("/admin/settings", headers=headers)
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["custodian_mode_enabled"] is True
    assert "platform_fee_percent" in body


async def test_patch_settings_updates_and_persists(client, db_session):
    headers = await _admin_headers(db_session)

    patch = await client.patch(
        "/admin/settings",
        json={"custodian_mode_enabled": True, "platform_fee_percent": "2.50"},
        headers=headers,
    )
    assert patch.status_code == 200
    assert patch.json()["data"]["custodian_mode_enabled"] is True
    assert patch.json()["data"]["platform_fee_percent"] == "2.50"

    get_again = await client.get("/admin/settings", headers=headers)
    assert get_again.json()["data"]["custodian_mode_enabled"] is True
    assert get_again.json()["data"]["platform_fee_percent"] == "2.50"


async def test_patch_settings_partial_update_leaves_other_field_untouched(client, db_session):
    headers = await _admin_headers(db_session)
    first = await client.patch("/admin/settings", json={"platform_fee_percent": "1.00"}, headers=headers)
    print("FIRST PATCH RESULT:", first.json())

    from sqlalchemy import select
    from app.modules.platform_settings.models import PlatformSettings

    rows = (await db_session.execute(select(PlatformSettings))).scalars().all()
    print("ROW COUNT AFTER FIRST PATCH:", len(rows), [(r.id, r.custodian_mode_enabled, r.platform_fee_percent) for r in rows])

    resp = await client.patch("/admin/settings", json={"custodian_mode_enabled": True}, headers=headers)
    assert resp.json()["data"]["custodian_mode_enabled"] is True
    assert resp.json()["data"]["platform_fee_percent"] == "1.00"


async def test_settings_requires_platform_admin(client, db_session):
    resp = await client.get("/admin/settings")
    assert resp.status_code == 401

    patch = await client.patch("/admin/settings", json={"custodian_mode_enabled": True})
    assert patch.status_code == 401
