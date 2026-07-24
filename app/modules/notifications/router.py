from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.auth import CurrentUser, get_current_group_admin_user
from app.core.config import settings
from app.core.db import get_db
from app.core.exceptions import BusinessRuleError, NotFoundError
from app.core.ratelimit import check_rate_limit
from app.core.redis import get_redis
from app.core.response import StandardResponse, success_response
from app.modules.group_admins.service import GroupAdminService
from app.modules.notifications.schemas import RemindPurseResponse
from app.modules.notifications.service import SendByteClient, get_sendbyte_client, send_purse_reminders
from app.modules.purses.models import Purse

router = APIRouter(prefix="/purses", tags=["notifications"])


@router.post("/{purse_id}/remind", response_model=StandardResponse[RemindPurseResponse])
async def remind_purse(
    purse_id: UUID,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(get_current_group_admin_user),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    sendbyte: SendByteClient = Depends(get_sendbyte_client),
) -> JSONResponse:
    # Row lock so two near-simultaneous requests for the same purse can't
    # both read a stale last_reminder_sent_at and both slip past the
    # cooldown check -- same class of race the payout balance checks guard
    # against elsewhere in this codebase.
    result = await db.execute(select(Purse).where(Purse.id == purse_id).with_for_update())
    purse = result.scalar_one_or_none()
    if purse is None:
        raise NotFoundError("purse not found")
    admin = await GroupAdminService(db).get_admin_for_group(current_user.id, purse.group_id)

    # Lightweight per-admin throttle against rapid retries/abuse -- the
    # substantive gate against exhausting the SendByte quota is the
    # per-purse weekly cooldown below, this just protects against a
    # burst of clicks/requests before that cooldown check even runs.
    await check_rate_limit(
        redis, "purses:remind", str(admin.id), settings.RATE_LIMIT_REMIND_PER_MINUTE, 60
    )

    if not settings.REMINDERS_ENABLED:
        raise BusinessRuleError("reminder emails are currently disabled", code="reminders_disabled")

    if purse.last_reminder_sent_at is not None:
        elapsed = datetime.now(timezone.utc) - purse.last_reminder_sent_at
        if elapsed < timedelta(days=settings.REMINDER_MIN_INTERVAL_DAYS):
            raise BusinessRuleError(
                f"a reminder was already sent for this purse within the last "
                f"{settings.REMINDER_MIN_INTERVAL_DAYS} days",
                code="reminder_too_soon",
            )

    purse.last_reminder_sent_at = datetime.now(timezone.utc)
    await db.commit()

    session_factory = async_sessionmaker(bind=db.bind, expire_on_commit=False)
    background_tasks.add_task(send_purse_reminders, purse_id, session_factory, sendbyte)

    return success_response({"purse_id": str(purse_id), "status": "reminders_queued"})
