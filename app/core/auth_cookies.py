import secrets

from fastapi.responses import JSONResponse

from app.core.config import settings

ACCESS_TOKEN_COOKIE = "access_token"
REFRESH_TOKEN_COOKIE = "refresh_token"
CSRF_COOKIE = "csrf_token"


def _secure() -> bool:
    # A real browser (and httpx's own cookie jar, which enforces this the
    # same way) refuses to ever send a Secure cookie back over a plain
    # http:// connection -- unconditionally hardcoding Secure=True would
    # make cookie mode silently impossible to exercise over local dev/test
    # http://localhost, not just unwise. Production is expected to serve
    # over https, so this only relaxes the flag for local development.
    return settings.ENV != "development"


def _cookie_kwargs(*, httponly: bool) -> dict:
    return {"httponly": httponly, "secure": _secure(), "samesite": "strict", "path": "/"}


def set_auth_cookies(response: JSONResponse, access_token: str, refresh_token: str) -> None:
    """Only called when USE_HTTPONLY_COOKIES is on. httpOnly so neither
    token is ever readable by JS -- the browser attaches them
    automatically on every same-site request instead."""
    response.set_cookie(
        ACCESS_TOKEN_COOKIE,
        access_token,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        **_cookie_kwargs(httponly=True),
    )
    response.set_cookie(
        REFRESH_TOKEN_COOKIE,
        refresh_token,
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        **_cookie_kwargs(httponly=True),
    )


def set_csrf_cookie(response: JSONResponse) -> None:
    """Deliberately NOT httpOnly -- the double-submit CSRF pattern only
    works because the frontend's own JS can read this value and echo it
    back as a header; a cross-site attacker's forged request carries the
    cookie automatically but has no way to read it, so it can't produce a
    matching header. Set once at login and left alone until logout --
    not rotated on every token refresh, since it was never compromised by
    a refresh happening."""
    response.set_cookie(
        CSRF_COOKIE,
        secrets.token_urlsafe(32),
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        **_cookie_kwargs(httponly=False),
    )


def clear_auth_cookies(response: JSONResponse) -> None:
    for name in (ACCESS_TOKEN_COOKIE, REFRESH_TOKEN_COOKIE, CSRF_COOKIE):
        response.delete_cookie(name, path="/")
