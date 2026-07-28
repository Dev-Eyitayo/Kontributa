from app.core.config import settings
from tests.conftest import find_redis_token
from tests.test_invites_members import _create_invite_link


async def _register_verify_login(client, monkeypatch, email="cookie-user@example.com"):
    """Registration and verification always go through the normal JSON
    flow (unaffected by USE_HTTPONLY_COOKIES) -- only login itself needs
    the flag on, so it's flipped on right before that call."""
    await client.post(
        "/auth/register",
        json={
            "email": email,
            "password": "P@ssword123",
            "first_name": "Cookie",
            "last_name": "User",
            "role": "member",
        },
    )
    verify_token = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"email": email, "token": verify_token})

    monkeypatch.setattr(settings, "USE_HTTPONLY_COOKIES", True)
    return await client.post("/auth/login", json={"email": email, "password": "P@ssword123"})


async def test_login_sets_httponly_cookies_and_omits_tokens_from_body(client, db_session, monkeypatch):
    resp = await _register_verify_login(client, monkeypatch)
    assert resp.status_code == 200, resp.text

    body = resp.json()["data"]
    assert body["role"] == "member"
    assert body.get("access_token") is None
    assert body.get("refresh_token") is None

    set_cookie_headers = resp.headers.get_list("set-cookie")
    by_name = {h.split("=", 1)[0]: h for h in set_cookie_headers}
    assert "access_token" in by_name and "httponly" in by_name["access_token"].lower()
    assert "refresh_token" in by_name and "httponly" in by_name["refresh_token"].lower()
    # csrf_token must be readable by JS -- NOT httponly -- or the
    # double-submit pattern can't work at all.
    assert "csrf_token" in by_name and "httponly" not in by_name["csrf_token"].lower()
    # Secure is deliberately env-conditional (see auth_cookies._secure) --
    # this whole suite runs with ENV=development, matching local dev, so
    # it's correctly absent here; the production case is covered by
    # test_secure_flag_set_when_not_development below.
    for name in ("access_token", "refresh_token", "csrf_token"):
        assert "samesite=strict" in by_name[name].lower()


async def test_secure_flag_set_when_not_development(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "ENV", "production")
    resp = await _register_verify_login(client, monkeypatch, email="cookie-prod@example.com")
    assert resp.status_code == 200, resp.text

    set_cookie_headers = resp.headers.get_list("set-cookie")
    by_name = {h.split("=", 1)[0]: h for h in set_cookie_headers}
    for name in ("access_token", "refresh_token", "csrf_token"):
        assert "secure" in by_name[name].lower()


async def test_cookie_mode_authenticates_protected_request_without_bearer_header(client, db_session, monkeypatch):
    await _register_verify_login(client, monkeypatch, email="cookie-me@example.com")

    # No Authorization header at all -- the client's cookie jar carries
    # the httpOnly access_token cookie automatically.
    resp = await client.get("/auth/me")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["email"] == "cookie-me@example.com"


async def test_cookie_mode_refresh_rotates_cookies_with_valid_csrf(client, db_session, monkeypatch):
    login = await _register_verify_login(client, monkeypatch, email="cookie-refresh@example.com")
    csrf_token = client.cookies.get("csrf_token")
    assert csrf_token

    resp = await client.post("/auth/refresh-token", json={}, headers={"X-CSRF-Token": csrf_token})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {}

    set_cookie_headers = resp.headers.get_list("set-cookie")
    names = {h.split("=", 1)[0] for h in set_cookie_headers}
    assert {"access_token", "refresh_token"} <= names

    # The rotated session still authenticates.
    me = await client.get("/auth/me")
    assert me.status_code == 200


async def test_cookie_mode_logout_clears_cookies(client, db_session, monkeypatch):
    await _register_verify_login(client, monkeypatch, email="cookie-logout@example.com")
    csrf_token = client.cookies.get("csrf_token")

    resp = await client.post("/auth/logout", json={}, headers={"X-CSRF-Token": csrf_token})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["logged_out"] is True

    # Cookies are gone from the client's own jar (server sent expiring
    # Set-Cookie headers), so the next request has nothing to authenticate with.
    assert client.cookies.get("access_token") is None
    me = await client.get("/auth/me")
    assert me.status_code == 401


async def test_cookie_mode_state_changing_request_rejected_without_csrf_header(client, db_session, monkeypatch):
    await _register_verify_login(client, monkeypatch, email="cookie-csrf@example.com")

    resp = await client.post("/auth/refresh-token", json={})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "csrf_token_invalid"


async def test_cookie_mode_state_changing_request_rejected_with_wrong_csrf_header(client, db_session, monkeypatch):
    await _register_verify_login(client, monkeypatch, email="cookie-csrf-wrong@example.com")

    resp = await client.post("/auth/refresh-token", json={}, headers={"X-CSRF-Token": "not-the-real-token"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "csrf_token_invalid"


async def test_cookie_mode_member_join_via_invite_is_csrf_exempt(client, db_session, monkeypatch):
    """A member accepting an invite link (POST /members/join/{token}) is
    their own sign-up-equivalent -- no session exists yet, so it can never
    carry a CSRF header, exactly like /auth/register. Regression test for
    a real production bug: this path was missing from CSRFMiddleware's
    exemptions (only /auth/* was covered), so real invite links 403'd with
    csrf_token_invalid the moment USE_HTTPONLY_COOKIES went on."""
    # The admin's own onboarding (register/login/create-invite-link) stays
    # in ordinary Bearer mode -- only flip the flag right before the
    # member's join call, the one this test actually exercises.
    token, _org, group = await _create_invite_link(client, db_session, admin_email="cookie-rep@example.com")
    monkeypatch.setattr(settings, "USE_HTTPONLY_COOKIES", True)

    join = await client.post(
        f"/members/join/{token}",
        json={
            "email": "cookie-member@example.com",
            "password": "P@ssword123",
            "first_name": "Cookie",
            "last_name": "Member",
        },
    )
    assert join.status_code == 201, join.text
    assert join.json()["data"]["group_id"] == str(group.id)


async def test_csrf_middleware_inactive_when_flag_off(client, db_session):
    """The flag defaults False for this whole suite -- a state-changing
    request with no CSRF header at all must NOT be rejected for that
    reason while cookie mode is off (Bearer mode has no CSRF exposure to
    mitigate in the first place)."""
    await client.post(
        "/auth/register",
        json={
            "email": "no-csrf-needed@example.com",
            "password": "P@ssword123",
            "first_name": "No",
            "last_name": "Csrf",
            "role": "member",
        },
    )
    verify_token = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"email": "no-csrf-needed@example.com", "token": verify_token})
    resp = await client.post(
        "/auth/login", json={"email": "no-csrf-needed@example.com", "password": "P@ssword123"}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["access_token"] is not None
