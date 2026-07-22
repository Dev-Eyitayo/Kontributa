import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import DateTime, Enum, ForeignKey, Numeric, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class PayoutStatus(str, enum.Enum):
    REQUESTED = "requested"
    APPROVED = "approved"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"


class PayoutActorType(str, enum.Enum):
    GROUP_ADMIN = "group_admin"
    PLATFORM_ADMIN = "platform_admin"
    WEBHOOK = "webhook"


class Payout(Base):
    __tablename__ = "payouts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("groups.id"), nullable=False, index=True
    )
    # Null = a sweep across the group's total available balance, not one purse.
    purse_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("purses.id"), nullable=True, index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[PayoutStatus] = mapped_column(
        Enum(PayoutStatus, name="payout_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=PayoutStatus.REQUESTED,
    )

    requested_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("group_admins.id"), nullable=False
    )
    approved_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )

    monnify_transfer_ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PayoutAllocation(Base):
    """Records how a group-wide sweep payout (Payout.purse_id is null) was
    proportionally attributed back to the individual purses it drew from,
    so a purse's available-balance calculation can account for money that
    left via a sweep rather than a purse-scoped payout."""

    __tablename__ = "payout_allocations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    payout_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payouts.id"), nullable=False, index=True
    )
    purse_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("purses.id"), nullable=False, index=True
    )
    allocated_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PayoutEvent(Base):
    __tablename__ = "payout_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    payout_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payouts.id"), nullable=False, index=True
    )
    from_status: Mapped[PayoutStatus] = mapped_column(
        Enum(PayoutStatus, name="payout_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    to_status: Mapped[PayoutStatus] = mapped_column(
        Enum(PayoutStatus, name="payout_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    actor_type: Mapped[PayoutActorType] = mapped_column(
        Enum(PayoutActorType, name="payout_actor_type", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    actor_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
