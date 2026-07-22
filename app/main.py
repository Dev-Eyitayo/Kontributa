import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError

from app.core.exceptions import AppException
from app.core.response import error_response
from app.modules.admin.router import router as admin_router
from app.modules.audit.router import router as audit_router
from app.modules.auth.router import router as auth_router
from app.modules.contributions.router import router as contributions_router
from app.modules.group_admins.router import router as group_admins_router
from app.modules.invites.router import router as invites_router
from app.modules.jobs.scheduler import start_scheduler, stop_scheduler
from app.modules.members.router import router as members_router
from app.modules.notifications.router import router as notifications_router
from app.modules.organizations.router import admin_router as organizations_admin_router
from app.modules.organizations.router import public_router as organizations_public_router
from app.modules.payouts.router import router as payouts_router
from app.modules.purses.router import router as purses_router
from app.modules.settlement.router import router as settlement_router
from app.modules.webhooks.router import router as webhooks_router

logger = logging.getLogger("kontributa")


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="Kontributa API", version="1.0.0", lifespan=lifespan)


@app.exception_handler(AppException)
async def app_exception_handler(request: Request, exc: AppException):
    return error_response(exc.code, exc.message, status_code=exc.status_code, details=exc.details)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return error_response(
        "validation_error",
        "request validation failed",
        status_code=422,
        details=exc.errors(),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("unhandled exception while processing %s %s", request.method, request.url.path)
    return error_response("internal_error", "an unexpected error occurred", status_code=500)


app.include_router(auth_router)
app.include_router(organizations_public_router)
app.include_router(organizations_admin_router)
app.include_router(group_admins_router)
app.include_router(invites_router)
app.include_router(members_router)
app.include_router(purses_router)
app.include_router(notifications_router)
app.include_router(contributions_router)
app.include_router(webhooks_router)
app.include_router(admin_router)
app.include_router(settlement_router)
app.include_router(payouts_router)
app.include_router(audit_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
