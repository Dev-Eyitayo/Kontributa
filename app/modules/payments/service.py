import base64
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.payments.schemas import (
    MonnifyAccountName,
    MonnifyInvoice,
    MonnifySubAccount,
    MonnifyTransactionStatus,
    MonnifyTransferResult,
)

# Confirmed against sandbox: invoice/create's expiryDate is read as
# Africa/Lagos (WAT, UTC+1, no DST) wall-clock time, not UTC -- an
# unconverted UTC timestamp is silently 1h "in the past" from Monnify's
# side, which sandbox rejects outright as "Invalid invoice expiry date"
# once the buffer is smaller than that offset.
_MONNIFY_TZ = ZoneInfo("Africa/Lagos")


class MonnifyError(AppException):
    status_code = 502
    code = "monnify_error"


def parse_monnify_datetime(value: str) -> Optional[datetime]:
    """Monnify is not consistent about this format across APIs: collection
    webhooks send `paidOn` as "2021-11-17 11:28:42.615" (with milliseconds,
    confirmed against Monnify's own webhook event-type docs), while
    disbursement webhooks send `createdOn`/`completedOn` as
    "17/03/2021 3:23:32 AM". Both are tried; unrecognized formats return
    None rather than raising, since a bad date shouldn't block processing
    the rest of the event."""
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%d/%m/%Y %I:%M:%S %p",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


class MonnifyClient:
    """
    Thin wrapper around Monnify's Dynamic Invoice + transaction status APIs.

    Confirmed directly against Monnify's own docs: the invoice/create
    request payload shape, the webhook signature algorithm
    (HMAC-SHA512(client secret key, raw request body)), the shared
    responseBody envelope, the Single Transfer request/response fields
    (including sourceAccountNumber, easy to miss and not optional), and
    collection-webhook paidOn's millisecond datetime format (distinct from
    disbursement webhooks' dd/MM/yyyy format -- parse_monnify_datetime
    handles both). The invoice/create *response* body shape (accountNumber,
    bankName, etc.) and the transaction-status query response were not
    shown in the docs reviewed -- still best-effort, worth one real
    sandbox call to confirm before depending on this in production.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        secret_key: str,
        contract_code: str,
        source_account_number: str = "",
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._secret_key = secret_key
        self._contract_code = contract_code
        self._source_account_number = source_account_number
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
        income_split_config: Optional[list[dict]] = None,
    ) -> MonnifyInvoice:
        body_payload = {
            "invoiceReference": invoice_reference,
            "amount": float(amount),
            "invoiceDescription": description,
            "currencyCode": "NGN",
            "contractCode": self._contract_code,
            "customerEmail": customer_email,
            "customerName": customer_name,
            "expiryDate": expires_at.astimezone(_MONNIFY_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        }
        # Direct-mode groups only -- routes their share of this specific
        # invoice straight to their own Monnify sub-account (see
        # create_sub_account below) rather than Kontributa's main wallet.
        # Contribution state transitions (pending/paid/expired/flagged) are
        # detected identically either way, off our own invoiceReference --
        # this only changes where the money settles, not how payment
        # confirmation works.
        if income_split_config:
            body_payload["incomeSplitConfig"] = income_split_config

        body = await self._request("POST", "/api/v1/invoice/create", body_payload)
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

    async def list_banks(self) -> list[dict]:
        """Full bank reference list from Monnify -- get_bank_name() below
        resolves a single code from this same call rather than duplicating
        the integration, and the /banks endpoint (app/modules/banks) uses
        this for its Redis-cached response."""
        banks = await self._request("GET", "/api/v1/banks")
        if isinstance(banks, dict):
            banks = banks.get("banks", [])
        return [{"bank_code": b.get("code", ""), "bank_name": b.get("name", "")} for b in banks]

    async def get_bank_name(self, bank_code: str) -> str:
        """Resolves a bank code to its display name via Monnify's bank
        reference-data list. Falls back to echoing the code if not found."""
        banks = await self.list_banks()
        for bank in banks:
            if bank["bank_code"] == bank_code:
                return bank["bank_name"]
        return bank_code

    async def verify_account_name(self, account_number: str, bank_code: str) -> MonnifyAccountName:
        """Resolves the real account holder name for a bank/account number --
        used to preview a settlement account before it's ever saved. This is
        a read-only lookup; nothing is persisted by calling it."""
        body = await self._request(
            "GET",
            f"/api/v1/disbursements/account/validate?accountNumber={account_number}&bankCode={bank_code}",
        )
        return MonnifyAccountName(
            account_number=body.get("accountNumber", account_number),
            bank_code=body.get("bankCode", bank_code),
            account_name=body.get("accountName", ""),
        )

    async def create_sub_account(
        self, bank_code: str, account_number: str, email: str, split_percentage: Decimal
    ) -> MonnifySubAccount:
        """Creates a Monnify sub-account for Direct-mode settlement -- a
        purse's split invoice (see create_invoice's income_split_config)
        routes the group's share straight here instead of Kontributa's main
        wallet. The account-name lookup (verify_account_name above) is
        reused as-is beforehand; this call only runs after that's already
        confirmed the account holder's name.

        Per Monnify's Create Sub-Account API this is a batch endpoint (an
        array of sub-account specs) even for a single one -- still
        best-effort like verify_account_name and create_invoice, worth one
        real sandbox call to confirm the exact response shape before this
        is offered as a live option (see known-limitations.md: Direct mode
        also depends on Monnify activating sub-account access on the
        account, an operational step outside this codebase).
        """
        body = await self._request(
            "POST",
            "/api/v1/sub-accounts",
            {
                "subAccounts": [
                    {
                        "currencyCode": "NGN",
                        "bankCode": bank_code,
                        "accountNumber": account_number,
                        "email": email,
                        "defaultSplitPercentage": float(split_percentage),
                    }
                ]
            },
        )
        accounts = body if isinstance(body, list) else body.get("subAccounts", [])
        first = accounts[0] if accounts else {}
        return MonnifySubAccount(
            sub_account_code=first.get("subAccountCode", ""),
            bank_code=first.get("bankCode", bank_code),
            account_number=first.get("accountNumber", account_number),
        )

    async def initiate_single_transfer(
        self,
        reference: str,
        amount: Decimal,
        bank_code: str,
        account_number: str,
        account_name: str,
        narration: str,
    ) -> MonnifyTransferResult:
        """Initiates a disbursement to a verified settlement account.

        Deliberately does not send any parameter that would bypass Monnify's
        disbursement OTP requirement -- that OTP step, external to this
        codebase, is a second manual control layered on top of the in-app
        admin approval gate, and is left exactly as Monnify configures it
        by default. Do not add one here as a "convenience".
        """
        body = await self._request(
            "POST",
            "/api/v2/disbursements/single",
            {
                "amount": float(amount),
                "reference": reference,
                "narration": narration,
                "destinationBankCode": bank_code,
                "destinationAccountNumber": account_number,
                "destinationAccountName": account_name,
                "currency": "NGN",
                "sourceAccountNumber": self._source_account_number,
            },
        )
        return MonnifyTransferResult(
            reference=body.get("reference", reference),
            status=body.get("status", "PENDING"),
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
    source_account_number=settings.MONNIFY_SOURCE_ACCOUNT_NUMBER,
)


def get_monnify_client() -> MonnifyClient:
    return monnify_client
