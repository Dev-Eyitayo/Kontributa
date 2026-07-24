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
        default=SettlementMode.CUSTODIAN,
    )
    # Only set in "direct" mode -- the Monnify sub-account code that a
    # purse's split invoice routes the group's share to.
    direct_sub_account_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
