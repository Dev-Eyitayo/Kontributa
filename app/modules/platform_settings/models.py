import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Numeric, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class PlatformSettings(Base):
    """Singleton row -- there is only ever one, fetched/created on demand
    by PlatformSettingsService.get_or_create() rather than seeded by
    migration, so there's no fixed id to depend on."""

    __tablename__ = "platform_settings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Kill switch for Custodian settlement mode. Off by default -- a fresh
    # deployment is Direct-only until a platform admin explicitly turns
    # Custodian back on. See docs/known-limitations.md for why Direct mode
    # itself still needs Monnify's sub-account feature activated before
    # this should be flipped on in production.
    custodian_mode_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    platform_fee_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=Decimal("0"))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
