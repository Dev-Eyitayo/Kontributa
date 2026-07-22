from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessRuleError
from app.modules.group_admins.models import GroupAdmin
from app.modules.payments.service import MonnifyClient
from app.modules.settlement.models import SettlementAccount


def _normalize(name: str) -> str:
    return " ".join(name.strip().lower().split())


class SettlementService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def lookup(self, monnify: MonnifyClient, bank_code: str, account_number: str) -> dict:
        resolved = await monnify.verify_account_name(account_number, bank_code)
        return {
            "account_name": resolved.account_name,
            "bank_code": resolved.bank_code,
            "account_number": resolved.account_number,
        }

    async def get(self, group_id: UUID) -> SettlementAccount | None:
        result = await self.db.execute(select(SettlementAccount).where(SettlementAccount.group_id == group_id))
        return result.scalar_one_or_none()

    async def save(
        self,
        monnify: MonnifyClient,
        admin: GroupAdmin,
        bank_code: str,
        account_number: str,
        confirmed_account_name: str,
    ) -> SettlementAccount:
        resolved = await monnify.verify_account_name(account_number, bank_code)

        if not resolved.account_name or _normalize(resolved.account_name) != _normalize(confirmed_account_name):
            # No override path -- a mismatch or failed lookup blocks saving outright.
            raise BusinessRuleError(
                "confirmed_account_name does not match the name Monnify resolved for this account",
                code="account_name_mismatch",
            )

        bank_name = await monnify.get_bank_name(resolved.bank_code)

        existing = await self.get(admin.group_id)
        now = datetime.now(timezone.utc)

        if existing is not None:
            existing.bank_code = resolved.bank_code
            existing.bank_name = bank_name
            existing.account_number = resolved.account_number
            existing.account_name_verified = True
            existing.verified_at = now
            existing.created_by_group_admin_id = admin.id
            account = existing
        else:
            account = SettlementAccount(
                group_id=admin.group_id,
                bank_code=resolved.bank_code,
                bank_name=bank_name,
                account_number=resolved.account_number,
                account_name_verified=True,
                verified_at=now,
                created_by_group_admin_id=admin.id,
            )
            self.db.add(account)

        await self.db.commit()
        await self.db.refresh(account)
        return account

    @staticmethod
    def mask_account_number(account_number: str) -> str:
        if len(account_number) <= 4:
            return account_number
        return "*" * (len(account_number) - 4) + account_number[-4:]
