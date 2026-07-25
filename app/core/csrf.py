import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.auth_cookies import CSRF_COOKIE
from app.core.config import settings
from app.core.response import error_response

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Endpoints that don't ride an existing authenticated session, so neither
# is a CSRF target: login/register/verify/forgot/reset predate any
# session (no csrf cookie could exist yet), and webhooks are
# server-to-server with their own signature-based auth, never carrying
# our cookies at all.
_EXEMPT_PATHS = {
    "/auth/login",
    "/auth/register",
    "/auth/verify-email",
    "/auth/resend-verification",
    "/auth/forgot-password",
    "/auth/reset-password",
}
_EXEMPT_PREFIXES = ("/webhooks",)


class CSRFMiddleware(BaseHTTPMiddleware):
    """Double-submit CSRF check, active only while USE_HTTPONLY_COOKIES is
    on. An httpOnly auth cookie is sent automatically by the browser on
    any request to this domain -- including one triggered by a
    malicious page the victim merely has open in another tab -- unlike a
    manually-attached Bearer header, which a cross-site attacker's page
    has no way to forge. Requires the request to also carry a header
    whose value matches the (JS-readable) csrf_token cookie; an attacker
    can ride the cookie but can't read it to produce the header."""

    async def dispatch(self, request: Request, call_next):
        if (
            settings.USE_HTTPONLY_COOKIES
            and request.method not in _SAFE_METHODS
            and request.url.path not in _EXEMPT_PATHS
            and not request.url.path.startswith(_EXEMPT_PREFIXES)
        ):
            cookie_token = request.cookies.get(CSRF_COOKIE)
            header_token = request.headers.get("x-csrf-token")
            if not cookie_token or not header_token or not secrets.compare_digest(cookie_token, header_token):
                return error_response("csrf_token_invalid", "missing or invalid CSRF token", status_code=403)

        return await call_next(request)
