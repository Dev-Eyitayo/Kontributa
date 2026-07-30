import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import httpx

from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.payments.base import (
    AccountNameResult,
    DirectPaymentProvider,
    InvoiceResult,
    SubAccountResult,
)
from app.modules.payments.schemas import MonnifyTransactionStatus

logger = logging.getLogger("kontributa.payments.paystack")


class PaystackError(AppException):
    status_code = 502
    code = "paystack_error"


class PaystackClient(DirectPaymentProvider):
    """
    Direct Mode Payment Provider implementation for Paystack API.
    """

    def __init__(self, base_url: str = "", secret_key: str = ""):
        self._base_url = (base_url or settings.PAYSTACK_BASE_URL).rstrip("/")
        self._secret_key = secret_key or settings.PAYSTACK_SECRET_KEY
        self._cached_banks: Optional[list[dict]] = None

    @property
    def provider_name(self) -> str:
        return "paystack"

    async def _request(
        self, method: str, path: str, json_body: Optional[dict] = None, params: Optional[dict] = None
    ) -> dict:
        secret_key = (self._secret_key or "").strip()
        if not secret_key:
            raise PaystackError(
                "Paystack secret key is missing. Please set PAYSTACK_SECRET_KEY."
            )

        full_url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {secret_key}",
            "Content-Type": "application/json",
        }

        # logger.debug(
        #     "Paystack API Request Details:\n"
        #     "  Action (Method): %s\n"
        #     "  Complete URL: %s\n"
        #     "  Headers: Authorization: Bearer <secret_key>\n"
        #     "  Request Body: %s",
        #     method,
        #     full_url,
        #     json.dumps(json_body, indent=2) if json_body is not None else "None",
        # )

        async with httpx.AsyncClient(base_url=self._base_url, timeout=15) as http:
            resp = await http.request(method, path, json=json_body, params=params, headers=headers)

        try:
            body = resp.json()
        except Exception:
            raise PaystackError(f"Paystack API returned non-JSON response: HTTP {resp.status_code}")

        if resp.status_code >= 400 or not body.get("status", False):
            msg = body.get("message", resp.text)
            raise PaystackError(f"Paystack API error on {path}: {msg}")

        return body.get("data", {})

    async def list_banks(self) -> list[dict]:
        """Returns list of banks formatted as [{'bank_code': ..., 'bank_name': ...}], deduplicated by bank_code."""
        if self._cached_banks is not None:
            return self._cached_banks

        data = await self._request("GET", "/bank?country=nigeria")
        seen_codes = set()
        banks = []
        for item in data:
            code = item.get("code", "").strip()
            name = item.get("name", "").strip()
            if code and code not in seen_codes:
                seen_codes.add(code)
                banks.append({
                    "bank_code": code,
                    "bank_name": name,
                })

        self._cached_banks = banks
        return banks

    async def get_bank_name(self, bank_code: str) -> str:
        """Resolves a bank code to its display name via Paystack's live bank list."""
        try:
            banks = await self.list_banks()
            for bank in banks:
                if bank.get("bank_code") == bank_code:
                    return bank.get("bank_name", bank_code)
        except Exception as exc:
            logger.warning("Paystack get_bank_name failed for %s: %s", bank_code, exc)

        return bank_code

    async def verify_account_name(self, account_number: str, bank_code: str) -> AccountNameResult:
        """Resolves real account holder name via Paystack bank resolve API."""
        data = await self._request(
            "GET",
            "/bank/resolve",
            params={"account_number": account_number, "bank_code": bank_code},
        )
        return AccountNameResult(
            account_number=data.get("account_number", account_number),
            bank_code=bank_code,
            account_name=data.get("account_name", ""),
        )

    async def create_sub_account(
        self, bank_code: str, account_number: str, email: str, split_percentage: Decimal
    ) -> SubAccountResult:
        """Creates a Paystack subaccount for Direct Mode revenue split."""
        payload = {
            "business_name": email or "Kontributa Direct Group",
            "settlement_bank": bank_code,
            "account_number": account_number,
            "percentage_charge": float(split_percentage),
            "primary_contact_email": email or "support@kontributa.app",
        }
        data = await self._request("POST", "/subaccount", json_body=payload)
        return SubAccountResult(
            sub_account_code=data.get("subaccount_code", ""),
            bank_code=data.get("settlement_bank", bank_code),
            account_number=data.get("account_number", account_number),
            payment_provider="paystack",
        )

    async def _get_or_create_customer(self, email: str, name: str) -> str:
        """Creates or fetches customer_code on Paystack for payment requests."""
        parts = name.strip().split(" ", 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ""
        payload = {
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
        }
        data = await self._request("POST", "/customer", json_body=payload)
        return data.get("customer_code", "")

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
        **kwargs: Any,
    ) -> InvoiceResult:
        """Creates a Paystack Payment Request (Invoice) with split payment to subaccount."""
        customer_code = await self._get_or_create_customer(customer_email, customer_name)
        amount_kobo = int(amount * 100)
        payload = {
            "customer": customer_code,
            "amount": amount_kobo,
            "description": description or f"Contribution {invoice_reference}",
            "due_date": expires_at.strftime("%Y-%m-%d"),
        }
        if sub_account_code:
            payload["subaccount"] = sub_account_code

        redirect_url = kwargs.get("redirect_url") or kwargs.get("callback_url")
        if not redirect_url and settings.FRONTEND_BASE_URL:
            redirect_url = f"{settings.FRONTEND_BASE_URL.rstrip('/')}/my-purses"
        if redirect_url:
            payload["redirect_url"] = redirect_url

        data = await self._request("POST", "/paymentrequest", json_body=payload)
        
        # Extract virtual account / offline payment details if present, or invoice code
        account_number = data.get("offline_reference", data.get("request_code", invoice_reference))
        
        return InvoiceResult(
            invoice_reference=data.get("request_code", invoice_reference),
            account_number=str(account_number),
            bank_name="Paystack Checkout / Dynamic Transfer",
            account_name=customer_name,
            amount=amount,
            expires_at=expires_at,
            payment_provider="paystack",
        )

    async def get_transaction_status(self, payment_reference: str) -> MonnifyTransactionStatus:
        """Queries transaction status by payment reference or Payment Request code via Paystack API."""
        try:
            if payment_reference.startswith("PRQ_"):
                data = await self._request("GET", f"/paymentrequest/{payment_reference}")
                raw_status = str(data.get("status", "")).lower()
                paid = data.get("paid") is True or raw_status in ("paid", "success")
                payment_status = "PAID" if paid else (raw_status.upper() or "PENDING")
                amount_kobo = data.get("amount_paid") or (data.get("amount") if paid else 0)
                amount_paid = Decimal(str(amount_kobo or 0)) / Decimal("100")
                paid_at_raw = data.get("paid_at") or data.get("updatedAt")
                paid_on = datetime.fromisoformat(paid_at_raw.replace("Z", "+00:00")) if paid_at_raw and paid else None
                return MonnifyTransactionStatus(
                    transaction_reference=str(data.get("id", payment_reference)),
                    payment_reference=payment_reference,
                    payment_status=payment_status,
                    amount_paid=amount_paid,
                    paid_on=paid_on,
                )
            else:
                data = await self._request("GET", f"/transaction/verify/{payment_reference}")
                status = data.get("status", "")
                payment_status = "PAID" if status == "success" else status.upper()
                amount_paid = Decimal(str(data.get("amount", 0))) / Decimal("100")
                paid_at_raw = data.get("paid_at") or data.get("paidAt")
                paid_on = datetime.fromisoformat(paid_at_raw.replace("Z", "+00:00")) if paid_at_raw else None
                return MonnifyTransactionStatus(
                    transaction_reference=str(data.get("id", payment_reference)),
                    payment_reference=payment_reference,
                    payment_status=payment_status,
                    amount_paid=amount_paid,
                    paid_on=paid_on,
                )
        except Exception as exc:
            logger.warning("Paystack get_transaction_status failed for %s: %s", payment_reference, exc)
            raise PaystackError(f"Failed to query Paystack transaction status: {exc}") from exc

    def verify_webhook_signature(self, raw_body: bytes, signature: str) -> bool:
        """Verifies x-paystack-signature header using HMAC-SHA512 with PAYSTACK_SECRET_KEY."""
        if not signature or not self._secret_key:
            return False
        expected = hmac.new(
            self._secret_key.encode("utf-8"),
            raw_body,
            hashlib.sha512,
        ).hexdigest()
        return hmac.compare_digest(expected.lower(), signature.lower())
