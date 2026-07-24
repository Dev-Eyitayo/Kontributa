import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Enum, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class AuditActorType(str, enum.Enum):
    GROUP_ADMIN = "group_admin"
    PLATFORM_ADMIN = "platform_admin"
    MEMBER = "member"
    WEBHOOK = "webhook"
    RECONCILIATION_JOB = "reconciliation_job"


class AuditLog(Base):
    """Universal, append-only record of every state-changing action
    platform-wide. record_event() in service.py is the only code path
    permitted to write here -- there is no update()/delete() on this model,
    and the database role the application runs as has UPDATE/DELETE
    revoked on this table at the schema level."""

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_type: Mapped[AuditActorType] = mapped_column(
        Enum(AuditActorType, name="audit_actor_type", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    # No FK -- polymorphic across group_admins/users/members, and null for
    # non-human actors (webhook, reconciliation_job).
    actor_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    before_state: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    after_state: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    prev_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    row_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditChainHead(Base):
    """A single-row pointer to the tip of the AuditLog hash chain. Exists
    purely as a lock target: record_event() takes SELECT ... FOR UPDATE on
    this row before computing a new row_hash, so two concurrent writers can
    never both chain off the same 'previous' row and silently fork the
    chain. Lives in its own table (not inside audit_log itself) specifically
    because audit_log has UPDATE revoked for the application's database
    role -- this table is mutable by design."""

    __tablename__ = "audit_chain_head"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_row_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
