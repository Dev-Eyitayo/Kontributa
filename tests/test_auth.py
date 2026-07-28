import asyncio
import json
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from tests.conftest import _state, find_redis_token


async def _get_family_id() -> str:
    """Reads the (sole, per test-isolation's flushall between tests)
    refresh token's family_id straight out of redis -- family_id is
    never returned by any API response, so this is the only way a test
    can get at it to manipulate last_active_at directly."""
    keys = await _state["redis"].keys("refresh:*")
    assert keys, "no refresh token found in redis"
    raw = await _state["redis"].get(keys[0])
    return json.loads(raw)["family_id"]


async def _set_last_active(family_id: str, when) -> None:
    await _state["redis"].set(f"last_active:{family_id}", str(int(when.timestamp())))


async def _get_last_active(family_id: str):
    raw = await _state["redis"].get(f"last_active:{family_id}")
    assert raw is not None, "no last_active_at recorded for this family"
    return datetime.fromtimestamp(int(raw), tz=timezone.utc)


async def _register(client, email="member1@example.com", role="member", password="P@ssword123"):
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
        "password": "P@ssword123",
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


async def test_register_rejects_weak_passwords(client):
    weak_passwords = ["12345678", "password", "Password123", "P@ssword", "p@ssword123"]
    for idx, weak_pwd in enumerate(weak_passwords):
        resp = await client.post(
            "/auth/register",
            json={
                "email": f"weak{idx}@example.com",
                "password": weak_pwd,
                "first_name": "Weak",
                "last_name": "Pass",
                "role": "member",
            },
        )
        assert resp.status_code == 422


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


async def test_get_me_returns_current_user(client):
    await _register(client, email="meendpoint@example.com", password="P@ssword123")
    verify_token = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"email": "meendpoint@example.com", "token": verify_token})
    login = await client.post(
        "/auth/login", json={"email": "meendpoint@example.com", "password": "P@ssword123"}
    )
    headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}

    resp = await client.get("/auth/me", headers=headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["email"] == "meendpoint@example.com"
    assert data["first_name"] == "Ada"
    assert data["last_name"] == "Lovelace"
    assert data["role"] == "member"
    assert data["is_platform_admin"] is False
    assert data["has_admin_identity"] is False
    assert data["has_member_identity"] is False

    unauth = await client.get("/auth/me")
    assert unauth.status_code == 401


async def test_unverified_account_cannot_log_in_group_admin_or_member(client):
    # Applies uniformly to both roles -- verification isn't role-specific.
    await _register(client, email="unverified-admin@example.com", role="group_admin", password="P@ssword123")
    admin_login = await client.post(
        "/auth/login", json={"email": "unverified-admin@example.com", "password": "P@ssword123"}
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

    await _register(client, email="unverified-member@example.com", role="member", password="P@ssword123")
    member_login = await client.post(
        "/auth/login", json={"email": "unverified-member@example.com", "password": "P@ssword123"}
    )
    assert member_login.status_code == 403
    assert member_login.json()["error"]["code"] == "email_not_verified"

    # Verifying flips it -- no tokens before, tokens after, same account.
    verify_token = await find_redis_token("verify_email")
    await client.post(
        "/auth/verify-email", json={"email": "unverified-member@example.com", "token": verify_token}
    )
    now_verified = await client.post(
        "/auth/login", json={"email": "unverified-member@example.com", "password": "P@ssword123"}
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
    await _register(client, email="resetme@example.com", password="oldP@ssword123")
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
        "/auth/login", json={"email": "resetme@example.com", "password": "oldP@ssword123"}
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
    await _register(client, email="logout-test@example.com", role="group_admin", password="P@ssword123")
    verify_token = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"email": "logout-test@example.com", "token": verify_token})
    login = await client.post(
        "/auth/login", json={"email": "logout-test@example.com", "password": "P@ssword123"}
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


async def test_refresh_token_ttl_is_driven_by_the_env_setting_not_hardcoded(client, monkeypatch):
    # A distinct value from both the real default (7) and the old
    # hardcoded one (30) -- if this shows up in redis, the TTL can only
    # have come from reading the setting at issue-time, not a literal.
    monkeypatch.setattr(settings, "REFRESH_TOKEN_EXPIRE_DAYS", 3)

    await _register(client, email="ttl-check@example.com", password="P@ssword123")
    verify_token = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"email": "ttl-check@example.com", "token": verify_token})
    login = await client.post("/auth/login", json={"email": "ttl-check@example.com", "password": "P@ssword123"})
    refresh_token = login.json()["data"]["refresh_token"]

    ttl_seconds = await _state["redis"].ttl(f"refresh:{refresh_token}")
    expected_seconds = 3 * 24 * 60 * 60
    # A few seconds of slack for however long the request itself took.
    assert expected_seconds - 5 <= ttl_seconds <= expected_seconds


async def test_heartbeat_updates_last_active_at_for_the_current_session(client):
    await _register(client, email="heartbeat@example.com", password="P@ssword123")
    verify_token = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"email": "heartbeat@example.com", "token": verify_token})
    login = await client.post("/auth/login", json={"email": "heartbeat@example.com", "password": "P@ssword123"})
    access_token = login.json()["data"]["access_token"]

    family_id = await _get_family_id()
    # Login itself seeds a baseline -- push it artificially into the past
    # so a genuine heartbeat call is the only thing that could move it
    # back to "now".
    await _set_last_active(family_id, datetime.now(timezone.utc) - timedelta(minutes=10))

    resp = await client.post("/auth/heartbeat", headers={"Authorization": f"Bearer {access_token}"})
    assert resp.status_code == 200
    assert resp.json()["data"]["ok"] is True

    last_active = await _get_last_active(family_id)
    assert datetime.now(timezone.utc) - last_active < timedelta(seconds=10)


async def test_refresh_is_rejected_after_the_inactivity_gap_exceeds_the_threshold(client):
    """The core inactivity-logout backstop: even a refresh token that is
    nowhere near its own absolute TTL must be rejected once too long has
    passed since the last *genuine* activity -- and the session must be
    fully revoked, not just this one request refused."""
    await _register(client, email="idle-out@example.com", password="P@ssword123")
    verify_token = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"email": "idle-out@example.com", "token": verify_token})
    login = await client.post("/auth/login", json={"email": "idle-out@example.com", "password": "P@ssword123"})
    refresh_token = login.json()["data"]["refresh_token"]

    family_id = await _get_family_id()
    # Well past INACTIVITY_TIMEOUT_MINUTES (30 by default), nowhere near
    # REFRESH_TOKEN_EXPIRE_DAYS -- this must fail specifically because of
    # the inactivity gap, not the token's own age.
    await _set_last_active(family_id, datetime.now(timezone.utc) - timedelta(minutes=45))

    resp = await client.post("/auth/refresh-token", json={"refresh_token": refresh_token})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "session_inactive"

    # The whole family must be gone -- a forced full re-login, not a
    # recoverable retry.
    assert await _state["redis"].get(f"refresh:{refresh_token}") is None


async def test_refresh_survives_light_but_real_activity_spaced_past_access_token_lifetime(client):
    """A person actively (if lightly) using the app must never be logged
    out by this mechanism -- simulated here by a last heartbeat 20
    minutes ago: well past the access token's own 15-minute lifetime
    (so a refresh is genuinely necessary), but comfortably inside the
    30-minute inactivity threshold."""
    await _register(client, email="still-active@example.com", password="P@ssword123")
    verify_token = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"email": "still-active@example.com", "token": verify_token})
    login = await client.post(
        "/auth/login", json={"email": "still-active@example.com", "password": "P@ssword123"}
    )
    refresh_token = login.json()["data"]["refresh_token"]

    family_id = await _get_family_id()
    await _set_last_active(family_id, datetime.now(timezone.utc) - timedelta(minutes=20))

    resp = await client.post("/auth/refresh-token", json={"refresh_token": refresh_token})
    assert resp.status_code == 200
    assert resp.json()["data"]["refresh_token"] != refresh_token


async def test_refresh_itself_never_counts_as_activity(client):
    """Regression test for the exact failure mode this whole mechanism
    exists to prevent: the silent access-token refresh fires on its own
    schedule regardless of whether a human is present, so it must never
    reset last_active_at -- otherwise an abandoned-but-open tab with any
    ambient background traffic would keep refreshing its own inactivity
    window forever and never actually time out."""
    await _register(client, email="no-self-credit@example.com", password="P@ssword123")
    verify_token = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"email": "no-self-credit@example.com", "token": verify_token})
    login = await client.post(
        "/auth/login", json={"email": "no-self-credit@example.com", "password": "P@ssword123"}
    )
    refresh_token = login.json()["data"]["refresh_token"]

    family_id = await _get_family_id()
    stale_but_valid = datetime.now(timezone.utc) - timedelta(minutes=20)
    await _set_last_active(family_id, stale_but_valid)

    resp = await client.post("/auth/refresh-token", json={"refresh_token": refresh_token})
    assert resp.status_code == 200

    last_active_after_refresh = await _get_last_active(family_id)
    # Compared as whole seconds -- the round trip through redis (stored
    # as an int() timestamp) drops sub-second precision.
    assert int(last_active_after_refresh.timestamp()) == int(stale_but_valid.timestamp())
