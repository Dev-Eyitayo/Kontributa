import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Enum, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class NotificationStatus(str, enum.Enum):
    SENT = "sent"
    FAILED = "failed"


class NotificationLog(Base):
    """Operational record of every email send attempt, success or failure --
    for debugging delivery problems, not a money/trust audit trail (that's
    Phase 6's AuditLog; this table is intentionally separate and does not
    need the same append-only/hash-chain guarantees)."""

    __tablename__ = "notification_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    to_email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    template_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[NotificationStatus] = mapped_column(
        Enum(NotificationStatus, name="notification_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    provider_message_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
