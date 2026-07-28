import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class SettlementMode(str, enum.Enum):
    CUSTODIAN = "custodian"
    DIRECT = "direct"


class SettlementAccount(Base):
    __tablename__ = "settlement_accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("groups.id"), unique=True, nullable=False, index=True
    )
    bank_code: Mapped[str] = mapped_column(String(10), nullable=False)
    bank_name: Mapped[str] = mapped_column(String(100), nullable=False)
    account_number: Mapped[str] = mapped_column(String(20), nullable=False)
    account_name_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_group_admin_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("group_admins.id"), nullable=False
    )
    # "custodian" (funds held, payout requested/approved -- the original,
    # only mode) or "direct" (Monnify sub-account split, funds never touch
    # Kontributa's wallet). Chosen explicitly at setup, no silent default.
    settlement_mode: Mapped[SettlementMode] = mapped_column(
        Enum(SettlementMode, name="settlement_mode", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=SettlementMode.DIRECT,
    )
    # Only set in "direct" mode -- the Monnify sub-account code that a
    # purse's split invoice routes the group's share to.
    direct_sub_account_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MonnifySettlementLog(Base):
    __tablename__ = "monnify_settlement_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    settlement_reference: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    sub_account_code: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    group_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("groups.id"), nullable=True, index=True
    )

    amount: Mapped[float] = mapped_column(nullable=False, default=0.0)
    fee: Mapped[float] = mapped_column(nullable=False, default=0.0)
    settled_amount: Mapped[float] = mapped_column(nullable=False, default=0.0)

    destination_account_name: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    destination_account_number: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    destination_bank_code: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    destination_bank_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    status: Mapped[str] = mapped_column(String(30), nullable=False, default="COMPLETED")
    settlement_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_payload: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

