from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import (
    AccessTokenBlacklist,
    CurrentUser,
    RefreshTokenService,
    SingleUseTokenStore,
    get_access_token_blacklist,
    get_current_user,
    get_email_verification_token_store,
    get_password_reset_token_store,
    get_refresh_token_service,
)
from app.core.config import settings
from app.core.db import get_db
from app.core.ratelimit import rate_limit_by_ip
from app.core.response import StandardResponse, success_response
from app.modules.auth.schemas import (
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    LoginRequest,
    LoginResponse,
    LogoutRequest,
    LogoutResponse,
    RefreshTokenRequest,
    RefreshTokenResponse,
    RegisterRequest,
    RegisterResponse,
    ResendVerificationRequest,
    ResendVerificationResponse,
    ResetPasswordRequest,
    ResetPasswordResponse,
    VerifyEmailRequest,
    VerifyEmailResponse,
)
from app.modules.auth.service import AuthService
from app.modules.notifications.service import NotificationService, SendByteClient, get_sendbyte_client

router = APIRouter(prefix="/auth", tags=["auth"])


def get_auth_service(
    db: AsyncSession = Depends(get_db),
    refresh_tokens: RefreshTokenService = Depends(get_refresh_token_service),
    blacklist: AccessTokenBlacklist = Depends(get_access_token_blacklist),
    verify_email_tokens: SingleUseTokenStore = Depends(get_email_verification_token_store),
    reset_password_tokens: SingleUseTokenStore = Depends(get_password_reset_token_store),
    sendbyte: SendByteClient = Depends(get_sendbyte_client),
) -> AuthService:
    return AuthService(
        db,
        refresh_tokens,
        blacklist,
        verify_email_tokens,
        reset_password_tokens,
        NotificationService(db, sendbyte),
    )


@router.post(
    "/register",
    status_code=201,
    response_model=StandardResponse[RegisterResponse],
    dependencies=[Depends(rate_limit_by_ip("auth:register", settings.RATE_LIMIT_REGISTER_PER_HOUR, 3600))],
)
async def register(payload: RegisterRequest, service: AuthService = Depends(get_auth_service)) -> JSONResponse:
    user = await service.register(payload)
    return success_response(
        {
            "id": str(user.id),
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "role": user.role.value,
            "verification_required": True,
        },
        status_code=201,
    )


@router.post(
    "/verify-email",
    response_model=StandardResponse[VerifyEmailResponse],
    dependencies=[
        Depends(rate_limit_by_ip("auth:verify-email", settings.RATE_LIMIT_VERIFY_EMAIL_PER_HOUR, 3600))
    ],
)
async def verify_email(
    payload: VerifyEmailRequest, service: AuthService = Depends(get_auth_service)
) -> JSONResponse:
    verified = await service.verify_email(payload.email, payload.token)
    return success_response({"verified": verified})


@router.post(
    "/resend-verification",
    response_model=StandardResponse[ResendVerificationResponse],
    dependencies=[
        Depends(rate_limit_by_ip("auth:resend-verification", settings.RATE_LIMIT_FORGOT_PASSWORD_PER_HOUR, 3600))
    ],
)
async def resend_verification(
    payload: ResendVerificationRequest, service: AuthService = Depends(get_auth_service)
) -> JSONResponse:
    await service.resend_verification(payload.email)
    return success_response({"message": "verification email sent if account exists and is unverified"})


@router.post("/login", response_model=StandardResponse[LoginResponse])
async def login(payload: LoginRequest, service: AuthService = Depends(get_auth_service)) -> JSONResponse:
    access_token, refresh_token, role = await service.login(payload)
    return success_response({"access_token": access_token, "refresh_token": refresh_token, "role": role})


@router.post("/refresh-token", response_model=StandardResponse[RefreshTokenResponse])
async def refresh_token(
    payload: RefreshTokenRequest, service: AuthService = Depends(get_auth_service)
) -> JSONResponse:
    access_token, new_refresh_token = await service.refresh(payload)
    return success_response({"access_token": access_token, "refresh_token": new_refresh_token})


@router.post("/logout", response_model=StandardResponse[LogoutResponse])
async def logout(
    payload: LogoutRequest,
    current_user: CurrentUser = Depends(get_current_user),
    service: AuthService = Depends(get_auth_service),
) -> JSONResponse:
    await service.logout(payload.refresh_token, current_user.jti, current_user.expires_at)
    return success_response({"logged_out": True})


@router.post(
    "/forgot-password",
    response_model=StandardResponse[ForgotPasswordResponse],
    dependencies=[
        Depends(rate_limit_by_ip("auth:forgot-password", settings.RATE_LIMIT_FORGOT_PASSWORD_PER_HOUR, 3600))
    ],
)
async def forgot_password(
    payload: ForgotPasswordRequest, service: AuthService = Depends(get_auth_service)
) -> JSONResponse:
    await service.forgot_password(payload.email)
    return success_response({"message": "reset link sent if account exists"})


@router.post(
    "/reset-password",
    response_model=StandardResponse[ResetPasswordResponse],
    dependencies=[
        Depends(rate_limit_by_ip("auth:reset-password", settings.RATE_LIMIT_RESET_PASSWORD_PER_HOUR, 3600))
    ],
)
async def reset_password(
    payload: ResetPasswordRequest, service: AuthService = Depends(get_auth_service)
) -> JSONResponse:
    await service.reset_password(payload)
    return success_response({"message": "password updated"})
