import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import DateTime, Enum, ForeignKey, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class ContributionStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    EXPIRED = "expired"
    FLAGGED_FOR_REVIEW = "flagged_for_review"
    PAID_MANUAL = "paid_manual"


class ActorType(str, enum.Enum):
    WEBHOOK = "webhook"
    RECONCILIATION_JOB = "reconciliation_job"
    REP_MANUAL = "rep_manual"


class Contribution(Base):
    __tablename__ = "contributions"
    __table_args__ = (UniqueConstraint("purse_id", "member_id", name="uq_contribution_purse_member"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    purse_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("purses.id"), nullable=False, index=True
    )
    member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("members.id"), nullable=False, index=True
    )

    invoice_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    account_number: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    bank_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    invoice_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    amount_expected: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    amount_received: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    status: Mapped[ContributionStatus] = mapped_column(
        Enum(ContributionStatus, name="contribution_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=ContributionStatus.PENDING,
        index=True,
    )
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ContributionEvent(Base):
    __tablename__ = "contribution_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contribution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contributions.id"), nullable=False, index=True
    )
    from_status: Mapped[ContributionStatus] = mapped_column(
        Enum(ContributionStatus, name="contribution_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    to_status: Mapped[ContributionStatus] = mapped_column(
        Enum(ContributionStatus, name="contribution_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    actor_type: Mapped[ActorType] = mapped_column(
        Enum(ActorType, name="actor_type", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    # No FK -- actor_id is polymorphic (a group_admins.id for rep_manual, null
    # for webhook/reconciliation_job since there's no single human actor).
    actor_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
