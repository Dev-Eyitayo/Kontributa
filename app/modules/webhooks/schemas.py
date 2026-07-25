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
class RejectedPaymentEventData:
    """Monnify's default behavior for a dynamic invoice: a transfer whose
    amount doesn't match the invoice exactly is rejected and reversed back
    to the sender, and this event fires instead of SUCCESSFUL_TRANSACTION
    -- no money was actually received, so processing this never changes a
    Contribution's status (it's still worth recording for visibility into
    why a member's transfer didn't register)."""

    payment_reference: str
    amount: Decimal
    rejection_reason: str
    expected_amount: Optional[Decimal]


@dataclass
class TransferEventData:
    reference: str
    success: bool
    reason: Optional[str]
