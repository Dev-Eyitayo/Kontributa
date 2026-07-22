from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ENV: str = "development"
    APP_BASE_URL: str = "http://localhost:8000"

    # Superuser connection -- used only for migrations and test schema bootstrap
    # (DDL, and granting/revoking the runtime role's privileges below).
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/kontributa"

    # The role the running application actually queries as. Deliberately not
    # the superuser: this is the role that has UPDATE/DELETE revoked on
    # audit_log (and contribution_events/payout_events) at the database
    # level, so that guarantee holds for every request the app ever serves,
    # not just in the ORM layer.
    RUNTIME_DATABASE_URL: str = "postgresql+asyncpg://kontributa_app:kontributa_app_password@localhost:5432/kontributa"
    APP_DB_ROLE: str = "kontributa_app"
    APP_DB_PASSWORD: str = "kontributa_app_password"

    REDIS_URL: str = "redis://localhost:6379/0"

    JWT_SECRET_KEY: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS: int = 24
    PASSWORD_RESET_TOKEN_EXPIRE_MINUTES: int = 60

    # Sandbox vs live is a config change only -- never a code change.
    MONNIFY_BASE_URL: str = "https://sandbox.monnify.com"
    MONNIFY_API_KEY: str = ""
    MONNIFY_SECRET_KEY: str = ""
    MONNIFY_CONTRACT_CODE: str = ""
    MONNIFY_INVOICE_EXPIRY_MINUTES: int = 60

    RECONCILIATION_INTERVAL_MINUTES: int = 20
    RECONCILIATION_PENDING_THRESHOLD_MINUTES: int = 60

    # Sandbox vs live is a config change only -- same pattern as Monnify above.
    SENDBYTE_BASE_URL: str = "https://api.sendbyte.africa"
    SENDBYTE_API_KEY: str = ""
    SENDBYTE_FROM_EMAIL: str = "noreply@kontributa.app"
    SENDBYTE_FROM_NAME: str = "Kontributa"

    # Global kill switch for the /purses/{id}/remind feature -- flip to False
    # via env var to stop reminder sends platform-wide without a code change.
    REMINDERS_ENABLED: bool = True
    REMINDER_MIN_INTERVAL_DAYS: int = 7

    # Fixed-window request caps on the endpoints that can trigger a SendByte
    # send, so a burst of traffic (or abuse) can't quietly exhaust the free
    # tier. Deliberately conservative defaults; raise via env var if needed.
    RATE_LIMIT_REGISTER_PER_HOUR: int = 10
    RATE_LIMIT_FORGOT_PASSWORD_PER_HOUR: int = 5
    RATE_LIMIT_REMIND_PER_MINUTE: int = 3


settings = Settings()
