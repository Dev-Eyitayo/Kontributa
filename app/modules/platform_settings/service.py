from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.platform_settings.models import PlatformSettings
from app.modules.platform_settings.schemas import UpdatePlatformSettingsRequest


class PlatformSettingsService:
    def __init__(self, db: AsyncSession):
        self.db = db

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

    async def update(self, payload: UpdatePlatformSettingsRequest) -> PlatformSettings:
        row = await self.get_or_create()
        if payload.custodian_mode_enabled is not None:
            row.custodian_mode_enabled = payload.custodian_mode_enabled
        if payload.platform_fee_percent is not None:
            row.platform_fee_percent = payload.platform_fee_percent
        await self.db.commit()
        await self.db.refresh(row)
        return row
