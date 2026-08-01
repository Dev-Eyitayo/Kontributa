from decimal import Decimal
import pytest
from httpx import AsyncClient

from app.modules.payments.base import AccountNameResult, SubAccountResult, InvoiceResult
from app.modules.payments.paystack import PaystackClient
from app.core.auth import create_access_token
from tests.conftest import create_platform_admin


@pytest.mark.asyncio
async def test_paystack_signature_verification():
    secret_key = "sk_test_123456789"
    client = PaystackClient(secret_key=secret_key)
    raw_body = b'{"event":"charge.success","data":{"reference":"ref_100"}}'
    import hmac, hashlib
    expected_signature = hmac.new(secret_key.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()

    assert client.verify_webhook_signature(raw_body, expected_signature) is True
    assert client.verify_webhook_signature(raw_body, "invalid_signature") is False


@pytest.mark.asyncio
async def test_platform_settings_gateway_toggles(client: AsyncClient, db_session):
    admin_headers = await _admin_platform_headers(db_session)

    # Initial settings -- both gateways on, Paystack active by default
    # (see PlatformSettings and migration f92c8a5b9903, which flipped the
    # default from Monnify).
    resp = await client.get("/admin/settings", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["monnify_enabled"] is True
    assert data["paystack_enabled"] is True
    assert data["active_payment_provider"] == "paystack"

    # Switch the active provider back to Monnify.
    patch_resp = await client.patch(
        "/admin/settings",
        json={"active_payment_provider": "monnify"},
        headers=admin_headers,
    )
    assert patch_resp.status_code == 200
    updated_data = patch_resp.json()["data"]
    assert updated_data["monnify_enabled"] is True
    assert updated_data["paystack_enabled"] is True
    assert updated_data["active_payment_provider"] == "monnify"


@pytest.mark.asyncio
async def test_platform_settings_rejects_disabling_both_gateways(client: AsyncClient, db_session):
    admin_headers = await _admin_platform_headers(db_session)

    patch_resp = await client.patch(
        "/admin/settings",
        json={"monnify_enabled": False, "paystack_enabled": False},
        headers=admin_headers,
    )
    assert patch_resp.status_code == 422
    assert "At least one payment provider" in patch_resp.text


async def _admin_platform_headers(db_session):
    admin = await create_platform_admin(db_session)
    token = create_access_token(admin.id, "group_admin")
    return {"Authorization": f"Bearer {token.token}"}
