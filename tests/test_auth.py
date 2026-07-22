from tests.conftest import find_redis_token


async def _register(client, email="member1@example.com", role="member", password="password123"):
    return await client.post(
        "/auth/register",
        json={
            "email": email,
            "password": password,
            "first_name": "Ada",
            "last_name": "Lovelace",
            "role": role,
        },
    )


async def test_register_success_envelope(client):
    resp = await _register(client)
    assert resp.status_code == 201
    body = resp.json()
    assert body["success"] is True
    assert body["error"] is None
    assert body["data"]["email"] == "member1@example.com"
    assert body["data"]["first_name"] == "Ada"
    assert body["data"]["last_name"] == "Lovelace"
    assert body["data"]["role"] == "member"
    assert body["data"]["verification_required"] is True


async def test_register_duplicate_email_error_envelope(client):
    await _register(client)
    resp = await _register(client)
    assert resp.status_code == 409
    body = resp.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "duplicate_email"


async def test_verify_email_success(client):
    await _register(client, email="verifyme@example.com")
    token = await find_redis_token("verify_email")

    resp = await client.post("/auth/verify-email", json={"token": token})
    assert resp.status_code == 200
    assert resp.json()["data"]["verified"] is True

    # token is single-use
    resp2 = await client.post("/auth/verify-email", json={"token": token})
    assert resp2.status_code == 401
    assert resp2.json()["error"]["code"] == "token_invalid"


async def test_login_success_and_invalid_credentials(client):
    await _register(client, email="loginme@example.com", password="correcthorse123")

    ok = await client.post(
        "/auth/login", json={"email": "loginme@example.com", "password": "correcthorse123"}
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["success"] is True
    assert "access_token" in body["data"]
    assert "refresh_token" in body["data"]
    assert body["data"]["role"] == "member"

    bad = await client.post(
        "/auth/login", json={"email": "loginme@example.com", "password": "wrongpassword"}
    )
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "invalid_credentials"


async def test_refresh_token_rotation_and_reuse_detection(client):
    await _register(client, email="rotator@example.com", password="correcthorse123")
    login = await client.post(
        "/auth/login", json={"email": "rotator@example.com", "password": "correcthorse123"}
    )
    refresh_token_1 = login.json()["data"]["refresh_token"]

    # First rotation succeeds and issues a new token.
    r1 = await client.post("/auth/refresh-token", json={"refresh_token": refresh_token_1})
    assert r1.status_code == 200
    refresh_token_2 = r1.json()["data"]["refresh_token"]
    assert refresh_token_2 != refresh_token_1

    # Reusing the already-rotated first token is a reuse/theft signal:
    # it must be rejected AND must revoke the whole token family.
    r2 = await client.post("/auth/refresh-token", json={"refresh_token": refresh_token_1})
    assert r2.status_code == 401
    assert r2.json()["error"]["code"] == "refresh_reuse_detected"

    # Because the family was revoked, even the legitimate rotated token
    # (refresh_token_2) must now be dead.
    r3 = await client.post("/auth/refresh-token", json={"refresh_token": refresh_token_2})
    assert r3.status_code == 401
    assert r3.json()["error"]["code"] == "refresh_invalid"


async def test_forgot_and_reset_password(client):
    await _register(client, email="resetme@example.com", password="oldpassword123")

    forgot = await client.post("/auth/forgot-password", json={"email": "resetme@example.com"})
    assert forgot.status_code == 200
    assert forgot.json()["success"] is True

    token = await find_redis_token("reset_password")
    reset = await client.post(
        "/auth/reset-password", json={"token": token, "new_password": "newpassword456"}
    )
    assert reset.status_code == 200

    old_login = await client.post(
        "/auth/login", json={"email": "resetme@example.com", "password": "oldpassword123"}
    )
    assert old_login.status_code == 401

    new_login = await client.post(
        "/auth/login", json={"email": "resetme@example.com", "password": "newpassword456"}
    )
    assert new_login.status_code == 200


async def test_forgot_password_unknown_email_still_returns_200(client):
    resp = await client.post("/auth/forgot-password", json={"email": "nobody@example.com"})
    assert resp.status_code == 200
    assert resp.json()["success"] is True


async def test_logout_revokes_refresh_token_and_blacklists_access_token(client):
    await _register(client, email="logout-test@example.com", role="group_admin", password="password123")
    login = await client.post(
        "/auth/login", json={"email": "logout-test@example.com", "password": "password123"}
    )
    access_token = login.json()["data"]["access_token"]
    refresh_token = login.json()["data"]["refresh_token"]

    # Valid token, just not onboarded yet -- proves the token is accepted
    # by the auth layer before logout.
    before = await client.get("/group-admins/me", headers={"Authorization": f"Bearer {access_token}"})
    assert before.status_code == 404

    logout = await client.post(
        "/auth/logout",
        json={"refresh_token": refresh_token},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert logout.status_code == 200
    assert logout.json()["data"]["logged_out"] is True

    after = await client.get("/group-admins/me", headers={"Authorization": f"Bearer {access_token}"})
    assert after.status_code == 401

    refresh_after_logout = await client.post("/auth/refresh-token", json={"refresh_token": refresh_token})
    assert refresh_after_logout.status_code == 401
