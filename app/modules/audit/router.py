from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_admin_user, get_current_user
from app.core.db import get_db
from app.core.exceptions import ForbiddenError
from app.core.response import success_response
from app.modules.audit.models import AuditLog
from app.modules.audit.service import AuditService
from app.modules.auth.models import User
from app.modules.group_admins.service import GroupAdminService
from app.modules.members.service import MemberService

router = APIRouter(prefix="/audit", tags=["audit"])


def get_audit_service(db: AsyncSession = Depends(get_db)) -> AuditService:
    return AuditService(db)


def _entry_out(entry: AuditLog) -> dict:
    return {
        "entity_type": entry.entity_type,
        "entity_id": str(entry.entity_id),
        "action": entry.action,
        "actor_type": entry.actor_type.value,
        "actor_id": str(entry.actor_id) if entry.actor_id else None,
        "before_state": entry.before_state,
        "after_state": entry.after_state,
        "created_at": entry.created_at.isoformat(),
    }


def _contribution_history_out(entry: AuditLog) -> dict:
    before = entry.before_state or {}
    after = entry.after_state or {}
    return {
        "from_status": before.get("status"),
        "to_status": after.get("status"),
        "actor_type": entry.actor_type.value,
        "actor_id": str(entry.actor_id) if entry.actor_id else None,
        "note": after.get("note"),
        "created_at": entry.created_at.isoformat(),
    }


def _payout_history_out(entry: AuditLog) -> dict:
    before = entry.before_state or {}
    after = entry.after_state or {}
    return {
        "from_status": before.get("status"),
        "to_status": after.get("status"),
        "actor_type": entry.actor_type.value,
        "actor_id": str(entry.actor_id) if entry.actor_id else None,
        "created_at": entry.created_at.isoformat(),
    }


@router.get("/contributions/{contribution_id}")
async def get_contribution_audit(
    contribution_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    service: AuditService = Depends(get_audit_service),
) -> JSONResponse:
    if current_user.role == "group_admin":
        admin = await GroupAdminService(db).get_by_user_id(current_user.id)
        entries = await service.contribution_history_for_admin(contribution_id, admin)
    else:
        member = await MemberService(db).get_by_user_id(current_user.id)
        entries = await service.contribution_history_for_member(contribution_id, member)

    return success_response([_contribution_history_out(e) for e in entries])


@router.get("/purses/{purse_id}")
async def get_purse_audit(
    purse_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    service: AuditService = Depends(get_audit_service),
) -> JSONResponse:
    if current_user.role != "group_admin":
        raise ForbiddenError("only a group admin can view a purse's audit history")

    admin = await GroupAdminService(db).get_by_user_id(current_user.id)
    entries = await service.purse_history_for_admin(purse_id, admin)
    return success_response([_entry_out(e) for e in entries])


@router.get("/payouts/{payout_id}")
async def get_payout_audit(
    payout_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    service: AuditService = Depends(get_audit_service),
) -> JSONResponse:
    # A platform admin's JWT role claim is still "group_admin" (it's the
    # is_platform_admin flag on User that actually distinguishes them, per
    # get_current_admin_user) -- so check that flag first, before assuming
    # a "group_admin"-role token belongs to a GroupAdmin with a group.
    user_row = await db.get(User, current_user.id)
    if user_row is not None and user_row.is_platform_admin:
        entries = await service.payout_history_for_platform_admin(payout_id)
    elif current_user.role == "group_admin":
        admin = await GroupAdminService(db).get_by_user_id(current_user.id)
        entries = await service.payout_history_for_admin(payout_id, admin)
    else:
        raise ForbiddenError("only a group admin or platform admin can view payout audit history")

    return success_response([_payout_history_out(e) for e in entries])


@router.get("/groups/{group_id}")
async def get_group_audit_feed(
    group_id: UUID,
    from_: Optional[datetime] = Query(default=None, alias="from"),
    to: Optional[datetime] = Query(default=None),
    _: CurrentUser = Depends(get_current_admin_user),
    service: AuditService = Depends(get_audit_service),
) -> JSONResponse:
    entries = await service.group_feed_for_platform_admin(group_id, from_, to)
    return success_response(
        [
            {
                "entity_type": e.entity_type,
                "entity_id": str(e.entity_id),
                "action": e.action,
                "actor_type": e.actor_type.value,
                "actor_id": str(e.actor_id) if e.actor_id else None,
                "created_at": e.created_at.isoformat(),
            }
            for e in entries
        ]
    )
