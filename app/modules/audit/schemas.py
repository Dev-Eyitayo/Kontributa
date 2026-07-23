from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel


class ContributionAuditEntry(BaseModel):
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    actor_type: str
    actor_id: Optional[UUID] = None
    note: Optional[str] = None
    created_at: datetime


class PurseAuditEntry(BaseModel):
    entity_type: str
    entity_id: UUID
    action: str
    actor_type: str
    actor_id: Optional[UUID] = None
    before_state: Optional[dict[str, Any]] = None
    after_state: Optional[dict[str, Any]] = None
    created_at: datetime


class PayoutAuditEntry(BaseModel):
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    actor_type: str
    actor_id: Optional[UUID] = None
    created_at: datetime


class GroupAuditFeedEntry(BaseModel):
    entity_type: str
    entity_id: UUID
    action: str
    actor_type: str
    actor_id: Optional[UUID] = None
    created_at: datetime
