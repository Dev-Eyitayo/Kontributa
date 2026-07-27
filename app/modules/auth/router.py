from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import (
    AccessTokenBlacklist,
    CurrentUser,
    RefreshTokenService,
    SessionActivityService,
    SingleUseTokenStore,
    get_access_token_blacklist,
    get_current_user,
    get_email_verification_token_store,
    get_password_reset_token_store,
    get_refresh_token_service,
    get_session_activity_service,
)
from app.core.auth_cookies import REFRESH_TOKEN_COOKIE, clear_auth_cookies, set_auth_cookies, set_csrf_cookie
from app.core.config import settings
from app.core.db import get_db
from app.core.exceptions import AuthError
from app.core.ratelimit import rate_limit_by_ip
from app.core.response import StandardResponse, success_response
from app.modules.auth.schemas import (
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    HeartbeatResponse,
    LoginRequest,
    LoginResponse,
    LogoutRequest,
    LogoutResponse,
    MeResponse,
    MyIdentityGroupItem,
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
    activity: SessionActivityService = Depends(get_session_activity_service),
) -> AuthService:
    return AuthService(
        db,
        refresh_tokens,
        blacklist,
        verify_email_tokens,
        reset_password_tokens,
        NotificationService(db, sendbyte),
        activity,
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


@router.get("/me", response_model=StandardResponse[MeResponse])
async def get_me(
    current_user: CurrentUser = Depends(get_current_user),
    service: AuthService = Depends(get_auth_service),
) -> JSONResponse:
    user, has_admin_identity, has_member_identity = await service.get_me(current_user.id)
    return success_response(
        {
            "id": str(user.id),
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "role": user.role.value,
            "is_platform_admin": user.is_platform_admin,
            "has_admin_identity": has_admin_identity,
            "has_member_identity": has_member_identity,
        }
    )


@router.get("/me/groups", response_model=StandardResponse[list[MyIdentityGroupItem]])
async def get_my_groups(
    current_user: CurrentUser = Depends(get_current_user),
    service: AuthService = Depends(get_auth_service),
) -> JSONResponse:
    """Every group this account is associated with, admin or member,
    combined into one list -- backs the frontend's unified group switcher
    (see current-groups.ts / UnifiedSwitcher.tsx)."""
    groups = await service.list_my_groups(current_user.id)
    return success_response(
        [
            {
                "group_id": str(g["group_id"]),
                "group_name": g["group_name"],
                "short_code": g["short_code"],
                "role": g["role"],
            }
            for g in groups
        ]
    )


@router.post("/login", response_model=StandardResponse[LoginResponse])
async def login(payload: LoginRequest, service: AuthService = Depends(get_auth_service)) -> JSONResponse:
    access_token, refresh_token, role = await service.login(payload)

    if settings.USE_HTTPONLY_COOKIES:
        response = success_response({"role": role})
        set_auth_cookies(response, access_token, refresh_token)
        set_csrf_cookie(response)
        return response

    return success_response({"access_token": access_token, "refresh_token": refresh_token, "role": role})


@router.post("/refresh-token", response_model=StandardResponse[RefreshTokenResponse])
async def refresh_token(
    request: Request,
    payload: RefreshTokenRequest,
    service: AuthService = Depends(get_auth_service),
) -> JSONResponse:
    if settings.USE_HTTPONLY_COOKIES:
        refresh_token_value = request.cookies.get(REFRESH_TOKEN_COOKIE)
    else:
        refresh_token_value = payload.refresh_token

    if not refresh_token_value:
        raise AuthError("missing refresh token", code="missing_token")

    access_token, new_refresh_token = await service.refresh(RefreshTokenRequest(refresh_token=refresh_token_value))

    if settings.USE_HTTPONLY_COOKIES:
        response = success_response({})
        set_auth_cookies(response, access_token, new_refresh_token)
        return response

    return success_response({"access_token": access_token, "refresh_token": new_refresh_token})


@router.post("/logout", response_model=StandardResponse[LogoutResponse])
async def logout(
    request: Request,
    payload: LogoutRequest,
    current_user: CurrentUser = Depends(get_current_user),
    service: AuthService = Depends(get_auth_service),
) -> JSONResponse:
    if settings.USE_HTTPONLY_COOKIES:
        refresh_token_value = request.cookies.get(REFRESH_TOKEN_COOKIE)
    else:
        refresh_token_value = payload.refresh_token

    if refresh_token_value:
        await service.logout(refresh_token_value, current_user.jti, current_user.expires_at)

    if settings.USE_HTTPONLY_COOKIES:
        response = success_response({"logged_out": True})
        clear_auth_cookies(response)
        return response

    return success_response({"logged_out": True})


@router.post("/heartbeat", response_model=StandardResponse[HeartbeatResponse])
async def heartbeat(
    current_user: CurrentUser = Depends(get_current_user),
    service: AuthService = Depends(get_auth_service),
) -> JSONResponse:
    # A deliberate signal of genuine human interaction -- the frontend
    # calls this only from real mouse/keyboard/touch/scroll listeners,
    # debounced to at most once a minute (see use-idle-session.ts). Never
    # inferred from ordinary API traffic, including the silent access-
    # token refresh itself, which fires on its own schedule regardless of
    # whether anyone is actually present.
    await service.heartbeat(current_user.family_id)
    return success_response({"ok": True})


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
