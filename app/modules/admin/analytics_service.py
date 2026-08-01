from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.contributions.models import ActorType, Contribution, ContributionEvent, ContributionStatus
from app.modules.members.models import Member
from app.modules.organizations.models import Group
from app.modules.purses.models import Purse
from app.modules.settlement.models import SettlementAccount

# Fixed "is this group active right now" definition -- deliberately
# independent of the ?days= reporting period a caller picks for the rest
# of the page. Reused verbatim by both get_overview()'s
# active_groups_count and list_groups_health()'s active/dormant split, so
# the two always agree with each other regardless of which endpoint is
# called with which `days` value.
GROUP_ACTIVITY_WINDOW_DAYS = 30

# needs-attention has no ?days= of its own (it's a single fixed-window
# snapshot, not a period the platform admin picks) -- both of its fields
# use this.
NEEDS_ATTENTION_WINDOW_DAYS = 30

_PAID_STATUSES = (ContributionStatus.PAID, ContributionStatus.PAID_MANUAL)
_CENTS = Decimal("0.01")


def _money(value: Optional[Decimal]) -> str:
    """Same wire convention as the rest of the app: a fixed 2dp decimal
    string, never a bare JSON number (see known-limitations.md). Values
    computed here (a SUM, or a SUM x percent / 100) can carry more than
    2 digits of precision before this rounds them back down."""
    amount = value if value is not None else Decimal("0")
    if not isinstance(amount, Decimal):
        amount = Decimal(str(amount))
    return str(amount.quantize(_CENTS, rounding=ROUND_HALF_UP))


def _percent(numerator: int, denominator: int) -> float:
    if not denominator:
        return 0.0
    return round((numerator / denominator) * 100, 2)


def _aware(dt: datetime) -> datetime:
    """SQLite (the test suite's dialect) doesn't preserve tzinfo on a
    DateTime(timezone=True) column the way Postgres does -- a value read
    back comes back naive even though it was written as UTC-aware. Same
    normalization idiom used elsewhere in this codebase (e.g.
    contributions/service.py's deadline handling) -- assume naive means
    UTC, since every datetime this app writes is UTC to begin with."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _build_buckets(days: int, now: datetime) -> list[tuple[datetime, datetime, str]]:
    """(bucket_start, bucket_end, label) tuples, oldest first, covering
    [now - days, now]. Daily buckets for a 7-day window; 7-day (weekly)
    buckets otherwise, so a 30- or 90-day window still renders as a
    readable handful of chart points rather than 30/90 individual bars.
    The very last bucket's end is nudged a second past `now` so a row
    whose timestamp is exactly `now` still lands inside it (every other
    bucket boundary is a plain half-open [start, end) interval)."""
    window_start = now - timedelta(days=days)
    bucket_days = 1 if days <= 7 else 7

    buckets: list[tuple[datetime, datetime, str]] = []
    cursor = window_start
    while cursor < now:
        bucket_end = min(cursor + timedelta(days=bucket_days), now)
        is_last = bucket_end >= now
        buckets.append((cursor, bucket_end + timedelta(seconds=1) if is_last else bucket_end, cursor.strftime("%b %d")))
        cursor = bucket_end
    return buckets


class AdminAnalyticsService:
    def __init__(self, db: AsyncSession):
        self.db = db

    def _is_active_expr(self, activity_window_start: datetime):
        """A group counts as active if it had a purse *created*, or a
        contribution *paid* (paid/paid_manual), within the activity
        window -- either signal alone is enough. Correlated EXISTS
        subqueries against the outer Group row, not a join, so a group
        with many purses/contributions is still counted once."""
        purse_exists = (
            select(Purse.id)
            .where(Purse.group_id == Group.id, Purse.created_at >= activity_window_start)
            .exists()
        )
        contribution_exists = (
            select(Contribution.id)
            .select_from(Contribution)
            .join(Purse, Contribution.purse_id == Purse.id)
            .where(
                Purse.group_id == Group.id,
                Contribution.status.in_(_PAID_STATUSES),
                Contribution.paid_at >= activity_window_start,
            )
            .exists()
        )
        return or_(purse_exists, contribution_exists)

    async def get_overview(self, days: int) -> dict:
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(days=days)
        activity_window_start = now - timedelta(days=GROUP_ACTIVITY_WINDOW_DAYS)

        # total_fee_revenue: Kontributa's cut of every paid/paid_manual
        # contribution in the window, using the fee percent that was
        # actually locked onto that contribution at invoice-generation
        # time (Contribution.platform_fee_percent_applied), not today's
        # PlatformSettings.platform_fee_percent -- a rate change must
        # never retroactively rewrite already-collected revenue. Null
        # (a paid_manual contribution that never had an invoice
        # generated at all -- see contributions/service.py::mark_manual)
        # contributes 0, not an error: no invoice, no locked-in split, no
        # fee was ever actually taken on that specific contribution.
        fee_expr = Contribution.amount_received * func.coalesce(Contribution.platform_fee_percent_applied, 0) / 100
        fee_stmt = select(func.coalesce(func.sum(fee_expr), 0)).where(
            Contribution.status.in_(_PAID_STATUSES),
            Contribution.paid_at >= window_start,
            Contribution.paid_at <= now,
        )
        total_fee_revenue = (await self.db.execute(fee_stmt)).scalar_one()

        # conversion_rate: of contributions whose invoice was ever
        # generated (invoice_id set) in the window, what fraction reached
        # `paid` (confirmed via the real online payment flow). Deliberately
        # excludes `paid_manual` from the numerator -- a manual mark means
        # the online invoice flow specifically did *not* convert (staff
        # had to record it out of band), which is exactly what this metric
        # is meant to surface, not paper over.
        invoiced_total = (
            await self.db.execute(
                select(func.count()).where(
                    Contribution.invoice_id.is_not(None),
                    Contribution.created_at >= window_start,
                    Contribution.created_at <= now,
                )
            )
        ).scalar_one()
        paid_total = (
            await self.db.execute(
                select(func.count()).where(
                    Contribution.invoice_id.is_not(None),
                    Contribution.status == ContributionStatus.PAID,
                    Contribution.created_at >= window_start,
                    Contribution.created_at <= now,
                )
            )
        ).scalar_one()
        conversion_rate = _percent(paid_total, invoiced_total)

        # active_groups_count: fixed GROUP_ACTIVITY_WINDOW_DAYS definition,
        # NOT the requested `days` -- "active right now" is a constant
        # yardstick, not something that should change shape depending on
        # which reporting period the admin happens to have selected.
        active_groups_count = (
            await self.db.execute(
                select(func.count()).select_from(Group).where(self._is_active_expr(activity_window_start))
            )
        ).scalar_one()

        # new_groups_count: created within the requested window.
        new_groups_count = (
            await self.db.execute(
                select(func.count()).where(Group.created_at >= window_start, Group.created_at <= now)
            )
        ).scalar_one()

        # retention_rate ("second-purse rate"): of groups old enough to
        # have had a fair `days`-long chance at it (created strictly
        # before the window started), what fraction have created more
        # than one purse -- ever, not just within the window. A group
        # created inside the window is excluded from the denominator
        # entirely, not counted as "not retained" -- it hasn't had time
        # to prove either way yet.
        eligible_total = (
            await self.db.execute(select(func.count()).where(Group.created_at < window_start))
        ).scalar_one()
        retained_groups_subq = (
            select(Purse.group_id)
            .join(Group, Purse.group_id == Group.id)
            .where(Group.created_at < window_start)
            .group_by(Purse.group_id)
            .having(func.count(Purse.id) > 1)
            .subquery()
        )
        retained_total = (
            await self.db.execute(select(func.count()).select_from(retained_groups_subq))
        ).scalar_one()
        retention_rate = _percent(retained_total, eligible_total)

        return {
            "total_fee_revenue": _money(total_fee_revenue),
            "conversion_rate": conversion_rate,
            "active_groups_count": active_groups_count,
            "new_groups_count": new_groups_count,
            "retention_rate": retention_rate,
        }

    async def get_revenue_trend(self, days: int) -> list[dict]:
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(days=days)
        buckets = _build_buckets(days, now)

        rows = (
            await self.db.execute(
                select(
                    Contribution.paid_at, Contribution.amount_received, Contribution.platform_fee_percent_applied
                ).where(
                    Contribution.status.in_(_PAID_STATUSES),
                    Contribution.paid_at >= window_start,
                    Contribution.paid_at <= now,
                )
            )
        ).all()

        result = []
        for start, end, label in buckets:
            bucket_total = sum(
                (amount_received * (fee_pct or Decimal(0)) / Decimal(100))
                for paid_at, amount_received, fee_pct in rows
                if start <= _aware(paid_at) < end
            ) or Decimal(0)
            result.append({"label": label, "amount": _money(bucket_total)})
        return result

    async def get_growth_trend(self, days: int) -> list[dict]:
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(days=days)
        buckets = _build_buckets(days, now)

        group_created_ats = (
            await self.db.execute(
                select(Group.created_at).where(Group.created_at >= window_start, Group.created_at <= now)
            )
        ).scalars().all()
        member_created_ats = (
            await self.db.execute(
                select(Member.created_at).where(Member.created_at >= window_start, Member.created_at <= now)
            )
        ).scalars().all()

        result = []
        for start, end, label in buckets:
            new_groups = sum(1 for created_at in group_created_ats if start <= _aware(created_at) < end)
            new_members = sum(1 for created_at in member_created_ats if start <= _aware(created_at) < end)
            result.append({"label": label, "new_groups": new_groups, "new_members": new_members})
        return result

    async def get_needs_attention(self) -> dict:
        now = datetime.now(timezone.utc)

        flagged_backlog_count = (
            await self.db.execute(
                select(func.count()).where(Contribution.status == ContributionStatus.FLAGGED_FOR_REVIEW)
            )
        ).scalar_one()

        # Trend: not a diff of the backlog count itself (a backlog is a
        # point-in-time snapshot with no "prior value" to diff against) --
        # instead, how many contributions *newly became* flagged_for_review
        # (ContributionEvent.to_status) in the last NEEDS_ATTENTION_WINDOW_DAYS
        # vs. the equivalent window before that. More new flags recently
        # than before = "increasing"; fewer = "decreasing"; equal = "stable".
        recent_start = now - timedelta(days=NEEDS_ATTENTION_WINDOW_DAYS)
        prior_start = now - timedelta(days=NEEDS_ATTENTION_WINDOW_DAYS * 2)
        recent_flags = (
            await self.db.execute(
                select(func.count()).where(
                    ContributionEvent.to_status == ContributionStatus.FLAGGED_FOR_REVIEW,
                    ContributionEvent.created_at >= recent_start,
                    ContributionEvent.created_at <= now,
                )
            )
        ).scalar_one()
        prior_flags = (
            await self.db.execute(
                select(func.count()).where(
                    ContributionEvent.to_status == ContributionStatus.FLAGGED_FOR_REVIEW,
                    ContributionEvent.created_at >= prior_start,
                    ContributionEvent.created_at < recent_start,
                )
            )
        ).scalar_one()
        trend: Literal["increasing", "decreasing", "stable"]
        if recent_flags > prior_flags:
            trend = "increasing"
        elif recent_flags < prior_flags:
            trend = "decreasing"
        else:
            trend = "stable"

        # webhook_rescue_rate: of contributions that reached `paid` in the
        # last NEEDS_ATTENTION_WINDOW_DAYS, what fraction were confirmed by
        # the reconciliation job (ContributionEvent.actor_type) rather than
        # the payment provider's webhook -- i.e. how often the safety net,
        # not the primary path, is what actually closed out the payment.
        paid_window_start = now - timedelta(days=NEEDS_ATTENTION_WINDOW_DAYS)
        paid_ids = (
            await self.db.execute(
                select(Contribution.id).where(
                    Contribution.status == ContributionStatus.PAID,
                    Contribution.paid_at >= paid_window_start,
                    Contribution.paid_at <= now,
                )
            )
        ).scalars().all()
        rescued = 0
        if paid_ids:
            rescued = (
                await self.db.execute(
                    select(func.count()).where(
                        ContributionEvent.contribution_id.in_(paid_ids),
                        ContributionEvent.to_status == ContributionStatus.PAID,
                        ContributionEvent.actor_type == ActorType.RECONCILIATION_JOB,
                    )
                )
            ).scalar_one()
        webhook_rescue_rate = _percent(rescued, len(paid_ids))

        return {
            "flagged_backlog_count": flagged_backlog_count,
            "flagged_backlog_trend": trend,
            "webhook_rescue_rate": webhook_rescue_rate,
        }

    async def get_provider_split(self, days: int) -> list[dict]:
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(days=days)
        results = []

        for provider in ("monnify", "paystack"):
            # Classified by each group's *current* SettlementAccount.payment_provider
            # -- there's no per-contribution provider field on Contribution
            # itself, so a group that has since migrated providers has its
            # entire history attributed to whichever provider it's on now,
            # not whichever was actually live when a given contribution's
            # invoice was generated. A known approximation, not a bug --
            # see docs/architecture-backend.md's settlement section.
            group_ids_subq = select(SettlementAccount.group_id).where(
                SettlementAccount.payment_provider == provider
            )

            volume_stmt = (
                select(func.coalesce(func.sum(Contribution.amount_received), 0))
                .select_from(Contribution)
                .join(Purse, Contribution.purse_id == Purse.id)
                .where(
                    Purse.group_id.in_(group_ids_subq),
                    Contribution.status.in_(_PAID_STATUSES),
                    Contribution.paid_at >= window_start,
                    Contribution.paid_at <= now,
                )
            )
            volume = (await self.db.execute(volume_stmt)).scalar_one()

            statuses = (
                await self.db.execute(
                    select(Contribution.status)
                    .select_from(Contribution)
                    .join(Purse, Contribution.purse_id == Purse.id)
                    .where(
                        Purse.group_id.in_(group_ids_subq),
                        Contribution.invoice_id.is_not(None),
                        Contribution.created_at >= window_start,
                        Contribution.created_at <= now,
                    )
                )
            ).scalars().all()
            paid_count = sum(1 for s in statuses if s == ContributionStatus.PAID)
            success_rate = _percent(paid_count, len(statuses))

            results.append({"provider": provider, "volume": _money(volume), "success_rate": success_rate})

        return results

    async def list_groups_health(
        self, status: Literal["active", "dormant", "new"], days: int, limit: int, offset: int
    ) -> tuple[list[dict], int]:
        now = datetime.now(timezone.utc)
        activity_window_start = now - timedelta(days=GROUP_ACTIVITY_WINDOW_DAYS)

        base_stmt = select(Group)
        if status == "active":
            base_stmt = base_stmt.where(self._is_active_expr(activity_window_start))
        elif status == "dormant":
            base_stmt = base_stmt.where(~self._is_active_expr(activity_window_start))
        else:  # "new" -- the only status where `days` matters, matching overview's new_groups_count.
            window_start = now - timedelta(days=days)
            base_stmt = base_stmt.where(Group.created_at >= window_start, Group.created_at <= now)

        total = (await self.db.execute(select(func.count()).select_from(base_stmt.subquery()))).scalar_one()

        stmt = base_stmt.order_by(Group.created_at.desc()).limit(limit).offset(offset)
        groups = (await self.db.execute(stmt)).scalars().all()
        group_ids = [g.id for g in groups]

        purse_counts: dict = {}
        last_purse_at: dict = {}
        member_counts: dict = {}
        last_paid_at: dict = {}

        if group_ids:
            purse_rows = await self.db.execute(
                select(Purse.group_id, func.count(), func.max(Purse.created_at))
                .where(Purse.group_id.in_(group_ids))
                .group_by(Purse.group_id)
            )
            for group_id, count, max_created_at in purse_rows.all():
                purse_counts[group_id] = count
                last_purse_at[group_id] = max_created_at

            member_rows = await self.db.execute(
                select(Member.group_id, func.count())
                .where(Member.group_id.in_(group_ids), Member.removed_at.is_(None))
                .group_by(Member.group_id)
            )
            member_counts = dict(member_rows.all())

            paid_rows = await self.db.execute(
                select(Purse.group_id, func.max(Contribution.paid_at))
                .select_from(Contribution)
                .join(Purse, Contribution.purse_id == Purse.id)
                .where(Purse.group_id.in_(group_ids), Contribution.status.in_(_PAID_STATUSES))
                .group_by(Purse.group_id)
            )
            last_paid_at = dict(paid_rows.all())

        items = []
        for g in groups:
            # last_activity_at: the more recent of "last purse created" /
            # "last contribution paid" -- the same two signals _is_active_expr
            # checks for existence of, surfaced here as actual timestamps.
            candidates = [t for t in (last_purse_at.get(g.id), last_paid_at.get(g.id)) if t is not None]
            last_activity_at = max(candidates) if candidates else None
            items.append(
                {
                    "id": str(g.id),
                    "name": g.name,
                    "last_activity_at": last_activity_at.isoformat() if last_activity_at else None,
                    "purse_count": purse_counts.get(g.id, 0),
                    "member_count": member_counts.get(g.id, 0),
                }
            )

        return items, total
