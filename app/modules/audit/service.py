import hashlib
import json
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, NotFoundError
from app.modules.audit.models import AuditActorType, AuditChainHead, AuditLog
from app.modules.auth.models import User
from app.modules.contributions.models import Contribution
from app.modules.group_admins.models import GroupAdmin
from app.modules.members.models import Member
from app.modules.payouts.models import Payout
from app.modules.purses.models import Purse

_AUTOMATED_ACTOR_LABELS = {
    AuditActorType.WEBHOOK: "Automated (webhook)",
    AuditActorType.RECONCILIATION_JOB: "Automated (reconciliation)",
}


def _serialize_state(state: Optional[dict]) -> str:
    return json.dumps(state, sort_keys=True, default=str) if state is not None else "null"


def _row_payload(
    entity_type: str,
    entity_id: UUID,
    action: str,
    actor_type: AuditActorType,
    actor_id: Optional[UUID],
    before_state: Optional[dict],
    after_state: Optional[dict],
    prev_hash: Optional[str],
) -> str:
    return "|".join(
        [
            entity_type,
            str(entity_id),
            action,
            actor_type.value if isinstance(actor_type, AuditActorType) else actor_type,
            str(actor_id) if actor_id else "",
            _serialize_state(before_state),
            _serialize_state(after_state),
            prev_hash or "",
        ]
    )


class AuditService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def record_event(
        self,
        entity_type: str,
        entity_id: UUID,
        action: str,
        actor_type: AuditActorType,
        actor_id: Optional[UUID],
        before_state: Optional[dict] = None,
        after_state: Optional[dict] = None,
    ) -> AuditLog:
        """The only place in the codebase that writes to audit_log. Does not
        commit -- participates in the caller's own transaction so the audit
        row and the business state change it describes land atomically
        together (or not at all).

        Locks the single AuditChainHead row for the duration: without this,
        two concurrent writers could each read the same 'current tip' row
        and legitimately chain their new row off of it, silently forking
        the hash chain in a way verify_chain() would then misreport as
        tampering."""
        head_result = await self.db.execute(select(AuditChainHead).where(AuditChainHead.id == 1).with_for_update())
        head = head_result.scalar_one()
        prev_hash = head.last_row_hash

        row_hash = hashlib.sha256(
            _row_payload(entity_type, entity_id, action, actor_type, actor_id, before_state, after_state, prev_hash).encode()
        ).hexdigest()

        entry = AuditLog(
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            actor_type=actor_type,
            actor_id=actor_id,
            before_state=before_state,
            after_state=after_state,
            prev_hash=prev_hash,
            row_hash=row_hash,
        )
        self.db.add(entry)
        head.last_row_hash = row_hash
        await self.db.flush()
        return entry

    async def verify_chain(self) -> dict[str, Any]:
        """Walks the whole table in insertion order and recomputes each
        row's hash from its own stored content plus the previous row's
        row_hash. Any row whose content was altered after being written --
        or any row spliced in directly via SQL with a guessed/incorrect
        hash -- fails to reproduce its stored row_hash, and every row after
        the tamper point fails too, since their prev_hash no longer matches
        reality."""
        result = await self.db.execute(select(AuditLog).order_by(AuditLog.created_at, AuditLog.id))
        rows = list(result.scalars().all())

        prev_hash: Optional[str] = None
        for row in rows:
            if row.prev_hash != prev_hash:
                return {"valid": False, "broken_at_id": str(row.id), "reason": "prev_hash does not match chain"}

            expected_hash = hashlib.sha256(
                _row_payload(
                    row.entity_type, row.entity_id, row.action, row.actor_type, row.actor_id,
                    row.before_state, row.after_state, prev_hash,
                ).encode()
            ).hexdigest()
            if row.row_hash != expected_hash:
                return {"valid": False, "broken_at_id": str(row.id), "reason": "row_hash does not match its content"}

            prev_hash = row.row_hash

        return {"valid": True, "broken_at_id": None, "reason": None}

    # -- Role-scoped reads -------------------------------------------------

    async def contribution_history_for_member(self, contribution_id: UUID, user_id: UUID) -> list[AuditLog]:
        contribution = await self.db.get(Contribution, contribution_id)
        if contribution is None:
            raise NotFoundError("contribution not found")
        # Resolved via the contribution's own member_id, then checked
        # against the caller -- resolving "the caller's member row" first
        # would pick arbitrarily among a multi-group member's several rows.
        member = await self.db.get(Member, contribution.member_id)
        if member is None or member.user_id != user_id:
            raise ForbiddenError("cannot view another member's contribution history")
        return await self._entity_history("contribution", contribution_id)

    async def contribution_history_for_admin(self, contribution_id: UUID, user_id: UUID) -> list[AuditLog]:
        # Imported locally to avoid a module-level cycle (group_admins does
        # not import audit, but keeping this import scoped here matches
        # audit/service.py's existing pattern of only touching other
        # modules' models, never their services, at module scope).
        from app.modules.group_admins.service import GroupAdminService

        contribution = await self.db.get(Contribution, contribution_id)
        if contribution is None:
            raise NotFoundError("contribution not found")
        purse = await self.db.get(Purse, contribution.purse_id)
        if purse is None:
            raise ForbiddenError("cannot view a contribution outside your own group's purses")
        await GroupAdminService(self.db).get_admin_for_group(user_id, purse.group_id)
        return await self._entity_history("contribution", contribution_id)

    async def payout_history_for_admin(self, payout_id: UUID, user_id: UUID) -> list[AuditLog]:
        from app.modules.group_admins.service import GroupAdminService

        payout = await self.db.get(Payout, payout_id)
        if payout is None:
            raise NotFoundError("payout not found")
        await GroupAdminService(self.db).get_admin_for_group(user_id, payout.group_id)
        return await self._entity_history("payout", payout_id)

    async def payout_history_for_platform_admin(self, payout_id: UUID) -> list[AuditLog]:
        payout = await self.db.get(Payout, payout_id)
        if payout is None:
            raise NotFoundError("payout not found")
        return await self._entity_history("payout", payout_id)

    async def purse_history_for_admin(self, purse_id: UUID, user_id: UUID) -> list[AuditLog]:
        """Full history for a purse: its own creation/edits/closures, plus
        every contribution and payout event tied to it -- the endpoint a
        treasury dispute gets resolved against."""
        from app.modules.group_admins.service import GroupAdminService

        purse = await self.db.get(Purse, purse_id)
        if purse is None:
            raise NotFoundError("purse not found")
        await GroupAdminService(self.db).get_admin_for_group(user_id, purse.group_id)

        contribution_ids_result = await self.db.execute(
            select(Contribution.id).where(Contribution.purse_id == purse_id)
        )
        contribution_ids = [row[0] for row in contribution_ids_result.all()]

        payout_ids_result = await self.db.execute(select(Payout.id).where(Payout.purse_id == purse_id))
        payout_ids = [row[0] for row in payout_ids_result.all()]

        conditions = [(AuditLog.entity_type == "purse") & (AuditLog.entity_id == purse_id)]
        if contribution_ids:
            conditions.append((AuditLog.entity_type == "contribution") & (AuditLog.entity_id.in_(contribution_ids)))
        if payout_ids:
            conditions.append((AuditLog.entity_type == "payout") & (AuditLog.entity_id.in_(payout_ids)))

        result = await self.db.execute(
            select(AuditLog).where(or_(*conditions)).order_by(AuditLog.created_at, AuditLog.id)
        )
        return list(result.scalars().all())

    async def group_feed_for_platform_admin(
        self, group_id: UUID, from_ts=None, to_ts=None, limit: int = 20, offset: int = 0
    ) -> tuple[list[AuditLog], int]:
        """Cross-entity audit feed for an entire group -- admin oversight
        view. Covers purse edits, and every contribution/payout/settlement
        event tied to a purse or payout in this group."""
        purse_ids_result = await self.db.execute(select(Purse.id).where(Purse.group_id == group_id))
        purse_ids = [row[0] for row in purse_ids_result.all()]

        contribution_ids: list[UUID] = []
        payout_ids: list[UUID] = []
        if purse_ids:
            contribution_ids_result = await self.db.execute(
                select(Contribution.id).where(Contribution.purse_id.in_(purse_ids))
            )
            contribution_ids = [row[0] for row in contribution_ids_result.all()]

            payout_ids_result = await self.db.execute(select(Payout.id).where(Payout.purse_id.in_(purse_ids)))
            payout_ids = [row[0] for row in payout_ids_result.all()]

        sweep_payout_ids_result = await self.db.execute(
            select(Payout.id).where(Payout.group_id == group_id, Payout.purse_id.is_(None))
        )
        payout_ids += [row[0] for row in sweep_payout_ids_result.all()]

        conditions = [
            (AuditLog.entity_type == "purse") & (AuditLog.entity_id.in_(purse_ids)) if purse_ids else None,
            (AuditLog.entity_type == "contribution") & (AuditLog.entity_id.in_(contribution_ids))
            if contribution_ids
            else None,
            (AuditLog.entity_type == "payout") & (AuditLog.entity_id.in_(payout_ids)) if payout_ids else None,
            (AuditLog.entity_type == "settlement_account") & (AuditLog.entity_id == group_id),
        ]
        conditions = [c for c in conditions if c is not None]

        stmt = select(AuditLog).where(or_(*conditions))
        if from_ts is not None:
            stmt = stmt.where(AuditLog.created_at >= from_ts)
        if to_ts is not None:
            stmt = stmt.where(AuditLog.created_at <= to_ts)

        total = (await self.db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        result = await self.db.execute(stmt.order_by(AuditLog.created_at, AuditLog.id).limit(limit).offset(offset))
        return list(result.scalars().all()), total

    async def _entity_history(self, entity_type: str, entity_id: UUID) -> list[AuditLog]:
        result = await self.db.execute(
            select(AuditLog)
            .where(AuditLog.entity_type == entity_type, AuditLog.entity_id == entity_id)
            .order_by(AuditLog.created_at, AuditLog.id)
        )
        return list(result.scalars().all())

    # -- Human-readable resolution for group-admin/member-facing responses --
    # A raw actor_id/entity_id means nothing to a Group Admin or Member --
    # only Platform Admin's own views (the cross-group feed) are allowed to
    # keep showing technical ids. Batched per distinct id rather than one
    # query per audit row, since a purse's full history can run to dozens
    # of entries.

    async def resolve_actor_names(self, entries: list[AuditLog]) -> dict[UUID, str]:
        group_admin_ids = {e.actor_id for e in entries if e.actor_type == AuditActorType.GROUP_ADMIN and e.actor_id}
        member_ids = {e.actor_id for e in entries if e.actor_type == AuditActorType.MEMBER and e.actor_id}
        platform_admin_user_ids = {
            e.actor_id for e in entries if e.actor_type == AuditActorType.PLATFORM_ADMIN and e.actor_id
        }

        user_ids_by_actor_id: dict[UUID, UUID] = {}
        if group_admin_ids:
            rows = (
                await self.db.execute(select(GroupAdmin.id, GroupAdmin.user_id).where(GroupAdmin.id.in_(group_admin_ids)))
            ).all()
            user_ids_by_actor_id.update({row[0]: row[1] for row in rows})
        if member_ids:
            rows = (
                await self.db.execute(select(Member.id, Member.user_id).where(Member.id.in_(member_ids)))
            ).all()
            user_ids_by_actor_id.update({row[0]: row[1] for row in rows})
        for actor_id in platform_admin_user_ids:
            user_ids_by_actor_id[actor_id] = actor_id

        all_user_ids = set(user_ids_by_actor_id.values())
        names_by_user_id: dict[UUID, str] = {}
        if all_user_ids:
            rows = (
                await self.db.execute(
                    select(User.id, User.first_name, User.last_name).where(User.id.in_(all_user_ids))
                )
            ).all()
            names_by_user_id = {row[0]: f"{row[1]} {row[2]}" for row in rows}

        result: dict[UUID, str] = {}
        for entry in entries:
            if entry.actor_id is None:
                continue
            if entry.actor_type in _AUTOMATED_ACTOR_LABELS:
                continue
            user_id = user_ids_by_actor_id.get(entry.actor_id)
            if user_id is not None and user_id in names_by_user_id:
                result[entry.actor_id] = names_by_user_id[user_id]
        return result

    @staticmethod
    def actor_label(entry: AuditLog, resolved_names: dict[UUID, str]) -> Optional[str]:
        if entry.actor_type in _AUTOMATED_ACTOR_LABELS:
            return _AUTOMATED_ACTOR_LABELS[entry.actor_type]
        if entry.actor_id is not None:
            return resolved_names.get(entry.actor_id)
        return None

    async def resolve_entity_labels(self, entries: list[AuditLog]) -> dict[UUID, str]:
        """Purse-history-only: an entry's entity_id is the purse's own id,
        a contribution id, or a payout id depending on entity_type -- none
        meaningful to a Group Admin as a bare UUID."""
        contribution_ids = {e.entity_id for e in entries if e.entity_type == "contribution"}
        payout_ids = {e.entity_id for e in entries if e.entity_type == "payout"}
        purse_ids = {e.entity_id for e in entries if e.entity_type == "purse"}

        labels: dict[UUID, str] = {}
        if contribution_ids:
            rows = (
                await self.db.execute(
                    select(Contribution.id, User.first_name, User.last_name)
                    .join(Member, Contribution.member_id == Member.id)
                    .join(User, Member.user_id == User.id)
                    .where(Contribution.id.in_(contribution_ids))
                )
            ).all()
            labels.update({row[0]: f"{row[1]} {row[2]}'s contribution" for row in rows})
        if payout_ids:
            rows = (
                await self.db.execute(select(Payout.id, Payout.amount).where(Payout.id.in_(payout_ids)))
            ).all()
            labels.update({row[0]: f"Payout of {row[1]}" for row in rows})
        if purse_ids:
            rows = (await self.db.execute(select(Purse.id, Purse.title).where(Purse.id.in_(purse_ids)))).all()
            labels.update({row[0]: row[1] for row in rows})
        return labels
