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


settings = Settings()
