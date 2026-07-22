import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider_event_id: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    raw_payload: Mapped[str] = mapped_column(Text, nullable=False)
    signature_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    processed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    processing_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
