from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional


@dataclass
class CollectionEventData:
    transaction_reference: str
    payment_reference: str
    amount_paid: Decimal
    payment_status: str
    paid_on: Optional[datetime]
