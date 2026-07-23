from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.contributions.models import Contribution, ContributionStatus
from app.modules.webhooks.models import WebhookEvent


class AdminService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_webhook_events(
        self, processed: Optional[bool] = None, limit: int = 20, offset: int = 0
    ) -> tuple[list[WebhookEvent], int]:
        stmt = select(WebhookEvent)
        if processed is not None:
            stmt = stmt.where(WebhookEvent.processed == processed)
        total = (await self.db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        result = await self.db.execute(stmt.order_by(WebhookEvent.received_at.desc()).limit(limit).offset(offset))
        return list(result.scalars().all()), total

    async def list_flagged_contributions(self, limit: int = 20, offset: int = 0) -> tuple[list[Contribution], int]:
        stmt = select(Contribution).where(Contribution.status == ContributionStatus.FLAGGED_FOR_REVIEW)
        total = (await self.db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        result = await self.db.execute(stmt.order_by(Contribution.updated_at).limit(limit).offset(offset))
        return list(result.scalars().all()), total
