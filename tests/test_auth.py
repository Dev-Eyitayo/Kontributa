import asyncio

from tests.conftest import _state, find_redis_token


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


async def test_simultaneous_duplicate_registration_returns_clean_409(client):
    # Both requests read "no existing user" before either commits -- the
    # real guarantee is the DB's unique constraint on users.email, caught
    # as an IntegrityError and turned into the same 409 the sequential
    # (non-racing) path returns, not an unhandled 500.
    payload = {
        "email": "race-condition@example.com",
        "password": "password123",
        "first_name": "Race",
        "last_name": "Condition",
        "role": "member",
    }
    results = await asyncio.gather(
        client.post("/auth/register", json=payload),
        client.post("/auth/register", json=payload),
    )

    statuses = sorted(r.status_code for r in results)
    assert statuses == [201, 409]

    loser = next(r for r in results if r.status_code == 409)
    assert loser.json()["error"]["code"] == "duplicate_email"


async def test_verify_email_success(client):
    await _register(client, email="verifyme@example.com")
    token = await find_redis_token("verify_email")

    resp = await client.post("/auth/verify-email", json={"email": "verifyme@example.com", "token": token})
    assert resp.status_code == 200
    assert resp.json()["data"]["verified"] is True

    # token is single-use
    resp2 = await client.post("/auth/verify-email", json={"email": "verifyme@example.com", "token": token})
    assert resp2.status_code == 401
    assert resp2.json()["error"]["code"] == "token_invalid"


async def test_verify_email_rejects_code_for_a_different_email(client):
    await _register(client, email="owner@example.com")
    token = await find_redis_token("verify_email")

    # A valid, unexpired code -- but presented with someone else's email.
    resp = await client.post("/auth/verify-email", json={"email": "not-the-owner@example.com", "token": token})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "token_invalid"

    # The real owner can still use it -- the mismatched attempt didn't burn it early.
    resp2 = await client.post("/auth/verify-email", json={"email": "owner@example.com", "token": token})
    assert resp2.status_code == 200
    assert resp2.json()["data"]["verified"] is True


async def test_resend_verification_issues_a_new_working_token(client):
    await _register(client, email="lost-my-token@example.com")
    first_token = await find_redis_token("verify_email")

    resend = await client.post("/auth/resend-verification", json={"email": "lost-my-token@example.com"})
    assert resend.status_code == 200
    assert resend.json()["success"] is True

    # resend doesn't invalidate the original token (either one still works
    # until whichever is used first, or both expire), so both keys coexist
    # in redis now -- find the new one specifically.
    keys = await _state["redis"].keys("verify_email:*")
    second_token = next(k.split(":", 1)[1] for k in keys if k.split(":", 1)[1] != first_token)

    verify = await client.post(
        "/auth/verify-email", json={"email": "lost-my-token@example.com", "token": second_token}
    )
    assert verify.status_code == 200
    assert verify.json()["data"]["verified"] is True


async def test_resend_verification_unknown_email_still_returns_200(client):
    resp = await client.post("/auth/resend-verification", json={"email": "nobody@example.com"})
    assert resp.status_code == 200
    assert resp.json()["success"] is True


async def test_resend_verification_already_verified_is_a_silent_noop(client):
    await _register(client, email="already-verified@example.com")
    token = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"email": "already-verified@example.com", "token": token})

    resp = await client.post("/auth/resend-verification", json={"email": "already-verified@example.com"})
    assert resp.status_code == 200

    # No new token should have been issued -- the redis key from the first
    # (already-consumed) token is gone, and nothing new was written.
    keys = await _state["redis"].keys("verify_email:*")
    assert keys == []


async def test_login_success_and_invalid_credentials(client):
    await _register(client, email="loginme@example.com", password="correcthorse123")
    verify_token = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"email": "loginme@example.com", "token": verify_token})

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


async def test_unverified_account_cannot_log_in_group_admin_or_member(client):
    # Applies uniformly to both roles -- verification isn't role-specific.
    await _register(client, email="unverified-admin@example.com", role="group_admin", password="password123")
    admin_login = await client.post(
        "/auth/login", json={"email": "unverified-admin@example.com", "password": "password123"}
    )
    assert admin_login.status_code == 403
    body = admin_login.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "email_not_verified"

    # Consume the admin's own token before registering the member below --
    # otherwise two unconsumed tokens coexist in redis and find_redis_token
    # (keys[0]) can't reliably tell them apart (see test_invites_members.py
    # for the same reasoning).
    admin_verify_token = await find_redis_token("verify_email")
    await client.post(
        "/auth/verify-email", json={"email": "unverified-admin@example.com", "token": admin_verify_token}
    )

    await _register(client, email="unverified-member@example.com", role="member", password="password123")
    member_login = await client.post(
        "/auth/login", json={"email": "unverified-member@example.com", "password": "password123"}
    )
    assert member_login.status_code == 403
    assert member_login.json()["error"]["code"] == "email_not_verified"

    # Verifying flips it -- no tokens before, tokens after, same account.
    verify_token = await find_redis_token("verify_email")
    await client.post(
        "/auth/verify-email", json={"email": "unverified-member@example.com", "token": verify_token}
    )
    now_verified = await client.post(
        "/auth/login", json={"email": "unverified-member@example.com", "password": "password123"}
    )
    assert now_verified.status_code == 200
    assert "access_token" in now_verified.json()["data"]


async def test_refresh_token_rotation_and_reuse_detection(client):
    await _register(client, email="rotator@example.com", password="correcthorse123")
    verify_token = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"email": "rotator@example.com", "token": verify_token})
    login = await client.post(
        "/auth/login", json={"email": "rotator@example.com", "password": "correcthorse123"}
    )
    refresh_token_1 = login.json()["data"]["refresh_token"]

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
    verify_token = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"email": "resetme@example.com", "token": verify_token})

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
    verify_token = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"email": "logout-test@example.com", "token": verify_token})
    login = await client.post(
        "/auth/login", json={"email": "logout-test@example.com", "password": "password123"}
    )
    access_token = login.json()["data"]["access_token"]
    refresh_token = login.json()["data"]["refresh_token"]

    # Valid token, just not onboarded yet -- proves the token is accepted
    # by the auth layer before logout (empty group list, not a 401).
    before = await client.get("/group-admins/me/groups", headers={"Authorization": f"Bearer {access_token}"})
    assert before.status_code == 200
    assert before.json()["data"] == []

    logout = await client.post(
        "/auth/logout",
        json={"refresh_token": refresh_token},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert logout.status_code == 200
    assert logout.json()["data"]["logged_out"] is True

    after = await client.get("/group-admins/me/groups", headers={"Authorization": f"Bearer {access_token}"})
    assert after.status_code == 401

    refresh_after_logout = await client.post("/auth/refresh-token", json={"refresh_token": refresh_token})
    assert refresh_after_logout.status_code == 401
