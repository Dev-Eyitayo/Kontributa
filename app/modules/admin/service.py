from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.contributions.models import Contribution, ContributionStatus
from app.modules.webhooks.models import WebhookEvent


class AdminService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_webhook_events(self, processed: Optional[bool] = None) -> list[WebhookEvent]:
        stmt = select(WebhookEvent)
        if processed is not None:
            stmt = stmt.where(WebhookEvent.processed == processed)
        result = await self.db.execute(stmt.order_by(WebhookEvent.received_at.desc()))
        return list(result.scalars().all())

    async def list_flagged_contributions(self) -> list[Contribution]:
        result = await self.db.execute(
            select(Contribution)
            .where(Contribution.status == ContributionStatus.FLAGGED_FOR_REVIEW)
            .order_by(Contribution.updated_at)
        )
        return list(result.scalars().all())
