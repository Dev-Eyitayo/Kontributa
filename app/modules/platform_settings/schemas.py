from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class PlatformSettingsResponse(BaseModel):
    custodian_mode_enabled: bool
    platform_fee_percent: str


class UpdatePlatformSettingsRequest(BaseModel):
    custodian_mode_enabled: Optional[bool] = None
    platform_fee_percent: Optional[Decimal] = None
