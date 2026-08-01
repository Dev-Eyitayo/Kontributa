import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request

from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.csrf import CSRFMiddleware
from app.core.exceptions import AppException
from app.core.logging import configure_logging
from app.core.response import error_response
from app.modules.admin.analytics_router import router as admin_analytics_router
from app.modules.admin.router import router as admin_router
from app.modules.audit.router import router as audit_router
from app.modules.auth.router import router as auth_router
from app.modules.banks.router import router as banks_router
from app.modules.contributions.router import router as contributions_router
from app.modules.group_admins.router import groups_router
from app.modules.group_admins.router import router as group_admins_router
from app.modules.invites.router import router as invites_router
from app.modules.jobs.scheduler import start_scheduler, stop_scheduler
from app.modules.members.router import router as members_router
from app.modules.notifications.router import router as notifications_router
from app.modules.organizations.router import admin_router as organizations_admin_router
from app.modules.organizations.router import public_router as organizations_public_router
from app.modules.payouts.router import router as payouts_router
from app.modules.platform_settings.router import router as platform_settings_router
from app.modules.purses.router import router as purses_router
from app.modules.realtime.router import router as realtime_router
from app.modules.settlement.router import router as settlement_router
from app.modules.webhooks.router import router as webhooks_router

configure_logging()

logger = logging.getLogger("kontributa")


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="Kontributa API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CSRFMiddleware)
# Added after CSRFMiddleware so it wraps outermost (Starlette runs the
# most-recently-added middleware first) -- CORS preflight (OPTIONS) and
# response headers need to be handled before/around everything else,
# including on error responses. allow_credentials is required for the
# httpOnly-cookie auth mode; with specific origins (never "*") that's
# allowed. See settings.ALLOWED_ORIGINS's own comment for when this
# actually matters.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(AppException)
async def app_exception_handler(request: Request, exc: AppException):
    response = error_response(exc.code, exc.message, status_code=exc.status_code, details=exc.details)
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is not None:
        response.headers["Retry-After"] = str(retry_after)
    return response



def _validation_message(errors: list) -> str:
    parts = []
    for err in errors:
        field = ".".join(str(p) for p in err.get("loc", []) if p != "body")
        parts.append(f"{field}: {err['msg']}" if field else err["msg"])
    return "; ".join(parts) if parts else "request validation failed"


def _sanitize_error_detail(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _sanitize_error_detail(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_error_detail(item) for item in obj]
    if isinstance(obj, Exception):
        return str(obj)
    return obj


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = _sanitize_error_detail(exc.errors())
    return error_response(
        "validation_error",
        _validation_message(errors),
        status_code=422,
        details=errors,
    )



@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("unhandled exception while processing %s %s", request.method, request.url.path)
    return error_response("internal_error", "an unexpected error occurred", status_code=500)


API_V1_PREFIX = "/api/v1"

app.include_router(auth_router, prefix=API_V1_PREFIX)
app.include_router(organizations_public_router, prefix=API_V1_PREFIX)
app.include_router(organizations_admin_router, prefix=API_V1_PREFIX)
app.include_router(group_admins_router, prefix=API_V1_PREFIX)
app.include_router(groups_router, prefix=API_V1_PREFIX)
app.include_router(invites_router, prefix=API_V1_PREFIX)
app.include_router(members_router, prefix=API_V1_PREFIX)
app.include_router(purses_router, prefix=API_V1_PREFIX)
app.include_router(notifications_router, prefix=API_V1_PREFIX)
app.include_router(contributions_router, prefix=API_V1_PREFIX)
app.include_router(admin_router, prefix=API_V1_PREFIX)
app.include_router(admin_analytics_router, prefix=API_V1_PREFIX)
app.include_router(settlement_router, prefix=API_V1_PREFIX)
app.include_router(payouts_router, prefix=API_V1_PREFIX)
app.include_router(audit_router, prefix=API_V1_PREFIX)
app.include_router(banks_router, prefix=API_V1_PREFIX)
app.include_router(platform_settings_router, prefix=API_V1_PREFIX)
app.include_router(realtime_router, prefix=API_V1_PREFIX)

# Deliberately NOT under /api/v1: webhooks is an external contract (the
# URL registered in the Monnify dashboard), not a client-facing API this
# app's own version bumps should touch; health is an infra probe (Render's
# healthCheckPath) that every version of this app must answer the same way.
app.include_router(webhooks_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
