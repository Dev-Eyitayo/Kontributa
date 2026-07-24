from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel

SettlementModeLiteral = Literal["direct", "custodian"]


class SettlementLookupRequest(BaseModel):
    bank_code: str
    account_number: str


class SettlementLookupResponse(BaseModel):
    account_name: str
    bank_code: str
    account_number: str


class SettlementSaveRequest(BaseModel):
    bank_code: str
    account_number: str
    confirmed_account_name: str


class SettlementAccountResponse(BaseModel):
    id: UUID
    bank_name: str
    account_number: str
    account_name_verified: bool
    verified_at: Optional[datetime] = None
    settlement_mode: SettlementModeLiteral
    # Only present in "direct" mode.
    direct_sub_account_code: Optional[str] = None


class SwitchSettlementModeRequest(BaseModel):
    new_mode: SettlementModeLiteral


class SwitchSettlementModeResponse(BaseModel):
    settlement_mode: SettlementModeLiteral


class SettlementOptionsResponse(BaseModel):
    custodian_mode_enabled: bool
