from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class PlatformSettingsResponse(BaseModel):
    custodian_mode_enabled: bool
    platform_fee_percent: str
    monnify_enabled: bool = True
    paystack_enabled: bool = True
    active_payment_provider: str = "paystack"


class UpdatePlatformSettingsRequest(BaseModel):
    custodian_mode_enabled: Optional[bool] = None
    platform_fee_percent: Optional[Decimal] = None
    monnify_enabled: Optional[bool] = None
    paystack_enabled: Optional[bool] = None
    active_payment_provider: Optional[str] = None
