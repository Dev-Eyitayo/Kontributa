from typing import Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    role: Literal["group_admin", "member"]


class RegisterResponse(BaseModel):
    id: UUID
    email: EmailStr
    first_name: str
    last_name: str
    role: str
    verification_required: bool = True


class VerifyEmailRequest(BaseModel):
    token: str


class VerifyEmailResponse(BaseModel):
    verified: bool


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    role: str


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class RefreshTokenResponse(BaseModel):
    access_token: str
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class LogoutResponse(BaseModel):
    logged_out: bool = True


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ForgotPasswordResponse(BaseModel):
    message: str = "reset link sent if account exists"


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8)


class ResetPasswordResponse(BaseModel):
    message: str = "password updated"
