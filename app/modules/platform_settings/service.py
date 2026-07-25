from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditActorType
from app.modules.audit.service import AuditService
from app.modules.platform_settings.models import PlatformSettings
from app.modules.platform_settings.schemas import UpdatePlatformSettingsRequest


class PlatformSettingsService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.audit = AuditService(db)

    async def get_or_create(self) -> PlatformSettings:
        result = await self.db.execute(select(PlatformSettings).limit(1))
        row = result.scalar_one_or_none()
        if row is not None:
            return row

        row = PlatformSettings()
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def update(self, payload: UpdatePlatformSettingsRequest, actor_id: UUID) -> PlatformSettings:
        row = await self.get_or_create()
        before_state = {
            "custodian_mode_enabled": row.custodian_mode_enabled,
            "platform_fee_percent": str(row.platform_fee_percent),
        }

        if payload.custodian_mode_enabled is not None:
            row.custodian_mode_enabled = payload.custodian_mode_enabled
        if payload.platform_fee_percent is not None:
            row.platform_fee_percent = payload.platform_fee_percent

        after_state = {
            "custodian_mode_enabled": row.custodian_mode_enabled,
            "platform_fee_percent": str(row.platform_fee_percent),
        }
        if after_state != before_state:
            await self.audit.record_event(
                entity_type="platform_settings",
                entity_id=row.id,
                action="platform_settings_updated",
                actor_type=AuditActorType.PLATFORM_ADMIN,
                actor_id=actor_id,
                before_state=before_state,
                after_state=after_state,
            )

        await self.db.commit()
        await self.db.refresh(row)
        return row
