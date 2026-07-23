from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class ReceivedResponse(BaseModel):
    received: bool = True


@dataclass
class CollectionEventData:
    transaction_reference: str
    payment_reference: str
    amount_paid: Decimal
    payment_status: str
    paid_on: Optional[datetime]


@dataclass
class TransferEventData:
    reference: str
    success: bool
    reason: Optional[str]
