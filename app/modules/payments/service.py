import base64
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

import httpx

from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.payments.schemas import MonnifyInvoice, MonnifyTransactionStatus


class MonnifyError(AppException):
    status_code = 502
    code = "monnify_error"


def parse_monnify_datetime(value: str) -> Optional[datetime]:
    for fmt in ("%d/%m/%Y %I:%M:%S %p", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


class MonnifyClient:
    """
    Thin wrapper around Monnify's Dynamic Invoice + transaction status APIs.

    Request/response field names follow Monnify's documented conventions
    (confirmed: webhook signature header/algorithm; best-effort from public
    integration references for the invoice create/status payload shapes,
    since Monnify's interactive API reference is a JS-rendered page this
    environment could not fully scrape) -- verify against a live sandbox
    call before depending on this in production.
    """

    def __init__(self, base_url: str, api_key: str, secret_key: str, contract_code: str):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._secret_key = secret_key
        self._contract_code = contract_code
        self._access_token: Optional[str] = None
        self._token_expires_at: Optional[datetime] = None

    async def _authenticate(self) -> str:
        if self._access_token and self._token_expires_at and datetime.now(timezone.utc) < self._token_expires_at:
            return self._access_token

        credentials = base64.b64encode(f"{self._api_key}:{self._secret_key}".encode()).decode()
        async with httpx.AsyncClient(base_url=self._base_url, timeout=15) as http:
            resp = await http.post("/api/v1/auth/login", headers={"Authorization": f"Basic {credentials}"})

        if resp.status_code != 200:
            raise MonnifyError(f"Monnify authentication failed: HTTP {resp.status_code}")

        response_body = resp.json().get("responseBody", {})
        token = response_body.get("accessToken")
        expires_in = response_body.get("expiresIn", 3600)
        if not token:
            raise MonnifyError("Monnify authentication response missing access token")

        self._access_token = token
        # Refresh a little early so we never use a token that expires mid-call.
        self._token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(expires_in - 60, 30))
        return token

    async def _request(self, method: str, path: str, json_body: Optional[dict] = None) -> dict:
        token = await self._authenticate()
        async with httpx.AsyncClient(base_url=self._base_url, timeout=15) as http:
            resp = await http.request(method, path, json=json_body, headers={"Authorization": f"Bearer {token}"})

        body = resp.json()
        if resp.status_code >= 400 or not body.get("requestSuccessful", False):
            raise MonnifyError(f"Monnify API error on {path}: {body.get('responseMessage', resp.text)}")
        return body.get("responseBody", {})

    async def create_invoice(
        self,
        invoice_reference: str,
        amount: Decimal,
        customer_name: str,
        customer_email: str,
        description: str,
        expires_at: datetime,
    ) -> MonnifyInvoice:
        body = await self._request(
            "POST",
            "/api/v1/invoice/create",
            {
                "invoiceReference": invoice_reference,
                "amount": float(amount),
                "invoiceDescription": description,
                "currencyCode": "NGN",
                "contractCode": self._contract_code,
                "customerEmail": customer_email,
                "customerName": customer_name,
                "expiryDate": expires_at.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        return MonnifyInvoice(
            invoice_reference=body["invoiceReference"],
            account_number=body["accountNumber"],
            bank_name=body["bankName"],
            account_name=body.get("accountName", customer_name),
            amount=Decimal(str(body.get("amount", amount))),
            expires_at=expires_at,
        )

    async def get_transaction_status(self, payment_reference: str) -> MonnifyTransactionStatus:
        """Queried by *our* payment reference (the one passed as
        invoiceReference at creation, stored as Contribution.invoice_id) --
        we never capture Monnify's own transactionReference since the only
        place we'd learn it is a webhook we may not have received, which is
        exactly the case reconciliation exists to cover."""
        body = await self._request(
            "GET", f"/api/v1/merchant/transactions/query?paymentReference={payment_reference}"
        )
        paid_on_raw = body.get("paidOn")
        return MonnifyTransactionStatus(
            transaction_reference=body.get("transactionReference", ""),
            payment_reference=body.get("paymentReference", payment_reference),
            payment_status=body.get("paymentStatus", ""),
            amount_paid=Decimal(str(body.get("amountPaid", "0"))),
            paid_on=parse_monnify_datetime(paid_on_raw) if paid_on_raw else None,
        )

    @staticmethod
    def verify_signature(raw_body: bytes, signature: str, secret_key: str) -> bool:
        """Monnify signs webhooks as HMAC-SHA512(client secret key, request body),
        sent in the `monnify-signature` header (production only -- sandbox does
        not sign notifications, per Monnify's docs)."""
        if not signature:
            return False
        computed = hmac.new(secret_key.encode(), raw_body, hashlib.sha512).hexdigest()
        return hmac.compare_digest(computed, signature)


monnify_client = MonnifyClient(
    base_url=settings.MONNIFY_BASE_URL,
    api_key=settings.MONNIFY_API_KEY,
    secret_key=settings.MONNIFY_SECRET_KEY,
    contract_code=settings.MONNIFY_CONTRACT_CODE,
)


def get_monnify_client() -> MonnifyClient:
    return monnify_client
