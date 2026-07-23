import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Enum, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class VerificationStatus(str, enum.Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    FLAGGED = "flagged"


class Member(Base):
    __tablename__ = "members"
    __table_args__ = (
        # A user may hold at most one Member row per group (not per platform
        # -- see MemberService.join_additional_group -- a group_admin or an
        # existing member of a different group can legitimately hold a
        # second Member row for a different group).
        UniqueConstraint("user_id", "group_id", name="uq_members_user_id_group_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("groups.id"), nullable=False, index=True
    )
    cohort: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    member_id_number: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    verification_status: Mapped[VerificationStatus] = mapped_column(
        Enum(VerificationStatus, name="verification_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=VerificationStatus.PENDING,
    )
    invite_source: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invite_links.id"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
