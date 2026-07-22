from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ENV: str = "development"
    APP_BASE_URL: str = "http://localhost:8000"

    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/kontributa"
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
