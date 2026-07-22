from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel


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
