import logging
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessRuleError, ForbiddenError, NotFoundError
from app.modules.audit.models import AuditActorType
from app.modules.audit.service import AuditService
from app.modules.auth.models import User
from app.modules.group_admins.models import GroupAdmin
from app.modules.payments.schemas import MonnifyAccountName
from app.modules.payments.service import MonnifyClient, MonnifyError
from app.modules.payouts.service import PayoutService
from app.modules.platform_settings.service import PlatformSettingsService
from app.modules.settlement.models import SettlementAccount, SettlementMode

logger = logging.getLogger("kontributa.settlement")

# Direct mode routes 100% of the split to the group's own sub-account --
# no platform fee was asked for in this prompt, so none is silently
# invented here. Raising this above 0 (and wiring a real fee decision) is
# a deliberate product choice for later, not a default to guess at now.
DIRECT_MODE_SPLIT_PERCENTAGE = Decimal("100")


def _normalize(name: str) -> str:
    return " ".join(name.strip().lower().split())


async def _verify_account_name(monnify: MonnifyClient, account_number: str, bank_code: str):
    """Wraps MonnifyClient.verify_account_name so the raw upstream error
    (which includes the request path/query string) never reaches a client
    response -- logged in full server-side, replaced with a clean, actionable
    message and a 4xx instead of MonnifyError's 502."""
    try:
        return await monnify.verify_account_name(account_number, bank_code)
    except MonnifyError as exc:
        logger.warning("account verification failed for %s/%s: %s", bank_code, account_number, exc)
        raise BusinessRuleError(
            "could not verify this account -- check that the account number and bank are correct",
            code="account_verification_failed",
        ) from exc


class SettlementService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.audit = AuditService(db)

    async def lookup(
        self, monnify: MonnifyClient, admin: GroupAdmin, bank_code: str, account_number: str
    ) -> dict:
        # Shared by both modes -- it's the same free Name Enquiry check
        # either way, nothing heavier is needed for direct.
        resolved = await _verify_account_name(monnify, account_number, bank_code)

        # Every verification attempt is a meaningful audit fact, even this
        # preview-only call that saves nothing -- logged and committed on
        # its own here since nothing else in this request touches the DB.
        await self.audit.record_event(
            entity_type="settlement_account",
            entity_id=admin.group_id,
            action="lookup_attempted",
            actor_type=AuditActorType.GROUP_ADMIN,
            actor_id=admin.id,
            before_state=None,
            after_state={
                "bank_code": bank_code,
                "account_number": account_number,
                "resolved_name": resolved.account_name,
            },
        )
        await self.db.commit()

        return {
            "account_name": resolved.account_name,
            "bank_code": resolved.bank_code,
            "account_number": resolved.account_number,
        }

    async def get(self, group_id: UUID) -> SettlementAccount | None:
        result = await self.db.execute(select(SettlementAccount).where(SettlementAccount.group_id == group_id))
        return result.scalar_one_or_none()

    async def _verify_and_confirm(
        self,
        monnify: MonnifyClient,
        admin: GroupAdmin,
        bank_code: str,
        account_number: str,
        confirmed_account_name: str,
    ) -> MonnifyAccountName:
        """The shared lookup-and-confirm-name step both modes' save paths
        go through -- no override path in either mode: a mismatch or
        failed lookup blocks saving outright, full stop."""
        resolved = await _verify_account_name(monnify, account_number, bank_code)

        if not resolved.account_name or _normalize(resolved.account_name) != _normalize(confirmed_account_name):
            # A failed verification attempt is itself a meaningful audit
            # fact, not noise to discard -- committed here on its own since
            # nothing else in this request is being persisted.
            await self.audit.record_event(
                entity_type="settlement_account",
                entity_id=admin.group_id,
                action="registration_rejected",
                actor_type=AuditActorType.GROUP_ADMIN,
                actor_id=admin.id,
                before_state=None,
                after_state={
                    "bank_code": bank_code,
                    "account_number": account_number,
                    "resolved_name": resolved.account_name,
                    "confirmed_name": confirmed_account_name,
                    "reason": "account_name_mismatch",
                },
            )
            await self.db.commit()
            raise BusinessRuleError(
                "confirmed_account_name does not match the name Monnify resolved for this account",
                code="account_name_mismatch",
            )

        return resolved

    async def _assert_custodian_mode_enabled(self, platform_settings: PlatformSettingsService) -> None:
        settings_row = await platform_settings.get_or_create()
        if not settings_row.custodian_mode_enabled:
            raise ForbiddenError(
                "custodian mode is currently disabled platform-wide -- Direct is the only "
                "settlement mode available right now",
                code="custodian_mode_disabled",
            )

    async def save(
        self,
        monnify: MonnifyClient,
        platform_settings: PlatformSettingsService,
        admin: GroupAdmin,
        bank_code: str,
        account_number: str,
        confirmed_account_name: str,
    ) -> SettlementAccount:
        """Custodian mode: funds are held, a payout is requested and
        approved through the existing payout flow. Unchanged behavior from
        before Direct mode existed -- this is just now one of two explicit
        choices instead of the only one, and only reachable while the
        platform-wide custodian_mode_enabled kill switch is on."""
        await self._assert_custodian_mode_enabled(platform_settings)
        resolved = await self._verify_and_confirm(monnify, admin, bank_code, account_number, confirmed_account_name)
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
            existing.settlement_mode = SettlementMode.CUSTODIAN
            existing.direct_sub_account_code = None
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
                settlement_mode=SettlementMode.CUSTODIAN,
            )
            self.db.add(account)

        await self.audit.record_event(
            entity_type="settlement_account",
            entity_id=admin.group_id,
            action="registration_saved",
            actor_type=AuditActorType.GROUP_ADMIN,
            actor_id=admin.id,
            before_state=None,
            after_state={"bank_code": resolved.bank_code, "account_number": resolved.account_number, "settlement_mode": "custodian"},
        )

        await self.db.commit()
        await self.db.refresh(account)
        return account

    async def save_direct(
        self,
        monnify: MonnifyClient,
        admin: GroupAdmin,
        bank_code: str,
        account_number: str,
        confirmed_account_name: str,
    ) -> SettlementAccount:
        """Direct mode: after the same name-verification as custodian mode,
        creates a Monnify sub-account and stores its code -- a purse's
        split invoice (see ContributionService.generate_invoice) routes
        the group's share straight there, never through Kontributa's
        wallet at all."""
        resolved = await self._verify_and_confirm(monnify, admin, bank_code, account_number, confirmed_account_name)
        bank_name = await monnify.get_bank_name(resolved.bank_code)

        admin_user = await self.db.get(User, admin.user_id)
        sub_account = await monnify.create_sub_account(
            bank_code=resolved.bank_code,
            account_number=resolved.account_number,
            email=admin_user.email if admin_user else "",
            split_percentage=DIRECT_MODE_SPLIT_PERCENTAGE,
        )

        existing = await self.get(admin.group_id)
        now = datetime.now(timezone.utc)

        if existing is not None:
            existing.bank_code = resolved.bank_code
            existing.bank_name = bank_name
            existing.account_number = resolved.account_number
            existing.account_name_verified = True
            existing.verified_at = now
            existing.created_by_group_admin_id = admin.id
            existing.settlement_mode = SettlementMode.DIRECT
            existing.direct_sub_account_code = sub_account.sub_account_code
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
                settlement_mode=SettlementMode.DIRECT,
                direct_sub_account_code=sub_account.sub_account_code,
            )
            self.db.add(account)

        await self.audit.record_event(
            entity_type="settlement_account",
            entity_id=admin.group_id,
            action="registration_saved",
            actor_type=AuditActorType.GROUP_ADMIN,
            actor_id=admin.id,
            before_state=None,
            after_state={
                "bank_code": resolved.bank_code,
                "account_number": resolved.account_number,
                "settlement_mode": "direct",
                "sub_account_code": sub_account.sub_account_code,
            },
        )

        await self.db.commit()
        await self.db.refresh(account)
        return account

    async def switch_mode(
        self,
        monnify: MonnifyClient,
        payout_service: PayoutService,
        platform_settings: PlatformSettingsService,
        admin: GroupAdmin,
        new_mode: SettlementMode,
    ) -> SettlementAccount:
        """custodian -> direct is blocked while custodian funds are still
        sitting uncollected (must be paid out through the existing payout
        flow first, so a switch never strands or silently forgets about
        them). direct -> custodian is allowed anytime -- direct mode never
        held any funds, so there's nothing to reconcile -- UNLESS the
        platform-wide custodian_mode_enabled kill switch is off, in which
        case direct -> custodian is blocked too (no group_id may end up in
        custodian mode while the switch is off, full stop).

        No new bank lookup is needed here (unlike save/save_direct) -- the
        account's bank details are already verified from whenever it was
        first saved; switching to direct just creates the sub-account
        against those same details."""
        account = await self.get(admin.group_id)
        if account is None:
            raise NotFoundError("no settlement account registered for this group")

        if account.settlement_mode == new_mode:
            return account

        if new_mode == SettlementMode.CUSTODIAN:
            await self._assert_custodian_mode_enabled(platform_settings)

        if new_mode == SettlementMode.DIRECT:
            balance = await payout_service.available_balance_for_group(admin.group_id)
            if balance > 0:
                raise BusinessRuleError(
                    f"cannot switch to direct mode -- {balance} is still available and uncollected "
                    "in custodian funds across this group's purses; request and complete a payout "
                    "for it first",
                    code="outstanding_custodian_balance",
                    details={"available_balance": str(balance)},
                )

            admin_user = await self.db.get(User, admin.user_id)
            sub_account = await monnify.create_sub_account(
                bank_code=account.bank_code,
                account_number=account.account_number,
                email=admin_user.email if admin_user else "",
                split_percentage=DIRECT_MODE_SPLIT_PERCENTAGE,
            )
            account.direct_sub_account_code = sub_account.sub_account_code
        else:
            account.direct_sub_account_code = None

        before_state = {"settlement_mode": account.settlement_mode.value}
        account.settlement_mode = new_mode
        await self.audit.record_event(
            entity_type="settlement_account",
            entity_id=admin.group_id,
            action="settlement_mode_changed",
            actor_type=AuditActorType.GROUP_ADMIN,
            actor_id=admin.id,
            before_state=before_state,
            after_state={"settlement_mode": new_mode.value},
        )
        await self.db.commit()
        await self.db.refresh(account)
        return account

    @staticmethod
    def mask_account_number(account_number: str) -> str:
        if len(account_number) <= 4:
            return account_number
        return "*" * (len(account_number) - 4) + account_number[-4:]
