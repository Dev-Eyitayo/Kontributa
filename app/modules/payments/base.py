from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import Optional, Union

from pydantic import BaseModel
from app.modules.payments.schemas import MonnifyTransactionStatus


class AccountNameResult(BaseModel):
    account_number: str
    bank_code: str
    account_name: str


class SubAccountResult(BaseModel):
    sub_account_code: str
    bank_code: str
    account_number: str
    payment_provider: str


class InvoiceResult(BaseModel):
    invoice_reference: str
    account_number: str
    bank_name: str
    account_name: str
    amount: Decimal
    expires_at: datetime
    payment_provider: str
    checkout_url: Optional[str] = None


class DirectPaymentProvider(ABC):
    """
    Abstract interface for Direct Mode Payment Providers (Monnify, Paystack, etc.).

    In Direct Mode, every group's contribution share is split and routed
    directly into their provider sub-account at invoice/checkout generation time.
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Returns provider identifier name ('monnify' or 'paystack')."""
        pass

    @abstractmethod
    async def list_banks(self) -> list[dict]:
        """Returns list of supported banks with 'bank_code' and 'bank_name'."""
        pass

    @abstractmethod
    async def get_bank_name(self, bank_code: str) -> str:
        """Resolves a bank code to its human-readable display name."""
        pass

    @abstractmethod
    async def verify_account_name(self, account_number: str, bank_code: str) -> AccountNameResult:
        """Resolves the real account holder name for a bank/account number."""
        pass

    @abstractmethod
    async def create_sub_account(
        self, bank_code: str, account_number: str, email: str, split_percentage: Decimal
    ) -> SubAccountResult:
        """Creates a provider sub-account for direct revenue settlement."""
        pass

    @abstractmethod
    async def create_invoice(
        self,
        invoice_reference: str,
        amount: Decimal,
        customer_name: str,
        customer_email: str,
        description: str,
        expires_at: datetime,
        income_split_config: Optional[list[dict]] = None,
        sub_account_code: Optional[str] = None,
        platform_fee_percent: Optional[Decimal] = None,
    ) -> InvoiceResult:
        """Generates a split-payment invoice/virtual account for a contribution."""
        pass

    @abstractmethod
    async def get_transaction_status(self, payment_reference: str) -> MonnifyTransactionStatus:
        """Queries transaction status by payment reference."""
        pass

    @abstractmethod
    def verify_webhook_signature(self, raw_body: bytes, signature: str) -> bool:
        """Verifies incoming webhook signature."""
        pass
