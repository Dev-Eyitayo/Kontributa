from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.core.security import validate_strong_password


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    role: Literal["group_admin", "member"]

    @field_validator("password")
    @classmethod
    def check_password_strength(cls, v: str) -> str:
        return validate_strong_password(v)


class RegisterResponse(BaseModel):
    id: UUID
    email: EmailStr
    first_name: str
    last_name: str
    role: str
    verification_required: bool = True


class MeResponse(BaseModel):
    id: UUID
    email: EmailStr
    first_name: str
    last_name: str
    role: str
    is_platform_admin: bool
    # Derived from live GroupAdmin/Member rows, not from role above -- see
    # AuthService.get_me.
    has_admin_identity: bool
    has_member_identity: bool


class MyIdentityGroupItem(BaseModel):
    """One row of the unified group switcher -- every group this account
    is associated with, admin or member, tagged with which. See
    AuthService.list_my_groups."""

    group_id: UUID
    group_name: str
    short_code: str
    role: Literal["admin", "member"]


class VerifyEmailRequest(BaseModel):
    email: EmailStr
    token: str


class VerifyEmailResponse(BaseModel):
    verified: bool


class ResendVerificationRequest(BaseModel):
    email: EmailStr


class ResendVerificationResponse(BaseModel):
    message: str = "verification email sent if account exists and is unverified"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    # Absent (None) when USE_HTTPONLY_COOKIES is on -- the tokens are set
    # as httpOnly cookies instead of being returned in the body. role is
    # always present either way, since the frontend needs it for
    # role-based routing regardless of auth mode.
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    role: str


class RefreshTokenRequest(BaseModel):
    # Optional -- in cookie mode the refresh token comes from the
    # httpOnly cookie instead, never a body field.
    refresh_token: Optional[str] = None


class RefreshTokenResponse(BaseModel):
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None


class LogoutRequest(BaseModel):
    refresh_token: Optional[str] = None


class LogoutResponse(BaseModel):
    logged_out: bool = True


class HeartbeatResponse(BaseModel):
    ok: bool = True


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ForgotPasswordResponse(BaseModel):
    message: str = "reset link sent if account exists"


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8)

    @field_validator("new_password")
    @classmethod
    def check_password_strength(cls, v: str) -> str:
        return validate_strong_password(v)


class ResetPasswordResponse(BaseModel):
    message: str = "password updated"
