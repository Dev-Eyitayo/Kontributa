from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _normalize_asyncpg_url(url: str) -> str:
    if not url or "://" not in url:
        return url

    scheme, _, rest = url.partition("://")
    if scheme in ("postgresql", "postgres"):
        url = f"postgresql+asyncpg://{rest}"

    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query))
    params.pop("channel_binding", None)
    if "sslmode" in params:
        params["ssl"] = params.pop("sslmode")
    if parts.hostname and "-pooler." in parts.hostname:
        params.setdefault("prepared_statement_cache_size", "0")

    return urlunsplit(parts._replace(query=urlencode(params)))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ENV: str = "development"
    FRONTEND_BASE_URL: str = "http://localhost:3000"

    ALLOWED_ORIGINS: str = ""
    LOG_LEVEL: str = "INFO"

    # Superuser connection -- used only for migrations and test schema bootstrap
    # (DDL, and granting/revoking the runtime role's privileges below).
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/kontributa"


    RUNTIME_DATABASE_URL: str = "postgresql+asyncpg://kontributa_app:kontributa_app_password@localhost:5432/kontributa"
    APP_DB_ROLE: str = "kontributa_app"
    APP_DB_PASSWORD: str = "kontributa_app_password"

    TEST_DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/kontributa_test"
    TEST_RUNTIME_DATABASE_URL: str = (
        "postgresql+asyncpg://kontributa_app:kontributa_app_password@localhost:5432/kontributa_test"
    )

    REDIS_URL: str = "redis://localhost:6379/0"

    JWT_SECRET_KEY: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    # A separate mechanism from the token's own absolute TTL above -- see
    # SessionActivityService. A refresh call is rejected once the gap
    # since the last genuine heartbeat exceeds this, even if the token
    # itself is still well within its REFRESH_TOKEN_EXPIRE_DAYS life.
    INACTIVITY_TIMEOUT_MINUTES: int = 30


    USE_HTTPONLY_COOKIES: bool = False

    EMAIL_VERIFICATION_TOKEN_EXPIRE_MINUTES: int = 10
    PASSWORD_RESET_TOKEN_EXPIRE_MINUTES: int = 10

    # Sandbox vs live is a config change only -- never a code change.
    MONNIFY_BASE_URL: str = "https://sandbox.monnify.com"
    MONNIFY_API_KEY: str = ""
    MONNIFY_SECRET_KEY: str = ""
    MONNIFY_CONTRACT_CODE: str = ""
    MONNIFY_INVOICE_EXPIRY_MINUTES: int = 30

    MONNIFY_SOURCE_ACCOUNT_NUMBER: str = ""

    PAYSTACK_BASE_URL: str = "https://api.paystack.co"
    PAYSTACK_SECRET_KEY: str = ""
    PAYSTACK_PUBLIC_KEY: str = ""

    RECONCILIATION_INTERVAL_MINUTES: int = 20
    RECONCILIATION_PENDING_THRESHOLD_MINUTES: int = 60


    ABLY_API_KEY: str = ""

    EMAIL_BASE_URL: str = "https://api.senviok.live"
    EMAIL_API_KEY: str = ""
    EMAIL_FROM_EMAIL: str = "noreply@kontributa.app"
    EMAIL_FROM_NAME: str = "Kontributa"
    REMINDERS_ENABLED: bool = True
    REMINDER_MIN_INTERVAL_DAYS: int = 7

    # Backward compatibility aliases for legacy SENDBYTE_* settings
    @property
    def SENDBYTE_BASE_URL(self) -> str:
        return self.EMAIL_BASE_URL

    @property
    def SENDBYTE_API_KEY(self) -> str:
        return self.EMAIL_API_KEY

    @property
    def SENDBYTE_FROM_EMAIL(self) -> str:
        return self.EMAIL_FROM_EMAIL

    @property
    def SENDBYTE_FROM_NAME(self) -> str:
        return self.EMAIL_FROM_NAME

    RATE_LIMIT_REGISTER_PER_HOUR: int = 10
    RATE_LIMIT_FORGOT_PASSWORD_PER_HOUR: int = 5
    RATE_LIMIT_REMIND_PER_MINUTE: int = 3
    RATE_LIMIT_VERIFY_EMAIL_PER_HOUR: int = 20
    RATE_LIMIT_RESET_PASSWORD_PER_HOUR: int = 20

    @field_validator(
        "DATABASE_URL", "RUNTIME_DATABASE_URL", "TEST_DATABASE_URL", "TEST_RUNTIME_DATABASE_URL"
    )
    @classmethod
    def _normalize_db_urls(cls, value: str) -> str:
        return _normalize_asyncpg_url(value)

    @property
    def cors_allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.ALLOWED_ORIGINS.split(",") if origin.strip()]


settings = Settings()
