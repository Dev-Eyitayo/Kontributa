import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Enum, ForeignKey, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class EnrollMode(str, enum.Enum):
    SNAPSHOT = "snapshot"
    AUTO_ENROLL = "auto_enroll"


class PurseStatus(str, enum.Enum):
    OPEN = "open"
    CLOSED = "closed"
    ARCHIVED = "archived"


class Purse(Base):
    __tablename__ = "purses"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("groups.id"), nullable=False, index=True
    )
    cohort: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_by_group_admin_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("group_admins.id"), nullable=False, index=True
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    enroll_mode: Mapped[EnrollMode] = mapped_column(
        Enum(EnrollMode, name="enroll_mode", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    status: Mapped[PurseStatus] = mapped_column(
        Enum(PurseStatus, name="purse_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=PurseStatus.OPEN,
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

