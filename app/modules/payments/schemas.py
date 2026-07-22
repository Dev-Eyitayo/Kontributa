from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass
class MonnifyInvoice:
    invoice_reference: str
    account_number: str
    bank_name: str
    account_name: str
    amount: Decimal
    expires_at: datetime


@dataclass
class MonnifyTransactionStatus:
    transaction_reference: str
    payment_reference: str
    payment_status: str
    amount_paid: Decimal
    paid_on: datetime | None


@dataclass
class MonnifyAccountName:
    account_number: str
    bank_code: str
    account_name: str


@dataclass
class MonnifyTransferResult:
    reference: str
    status: str
