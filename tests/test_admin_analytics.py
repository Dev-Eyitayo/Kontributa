"""GET /admin/analytics/* -- every field returned is precomputed
server-side (money strings, percentages, trend labels), never raw
numbers the frontend would need to reduce/divide itself. These tests
build a small, hand-computable dataset directly against the DB (the
usual pattern here for states the API surface itself can't easily
reach) and check the endpoints' arithmetic against independently
hand-computed expectations, not just response shape.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from app.core.auth import create_access_token
from app.modules.contributions.models import ActorType, Contribution, ContributionEvent, ContributionStatus
from app.modules.group_admins.models import GroupAdmin
from app.modules.members.models import Member
from app.modules.organizations.models import Group
from app.modules.purses.models import EnrollMode, Purse, PurseStatus
from app.modules.settlement.models import SettlementAccount, SettlementMode
from tests.conftest import create_platform_admin


def _now():
    return datetime.now(timezone.utc)


async def _admin_headers(db_session):
    admin = await create_platform_admin(db_session, email=f"admin-{uuid4().hex[:8]}@example.com")
    token = create_access_token(admin.id, "group_admin")
    return {"Authorization": f"Bearer {token.token}"}


async def _make_group(db_session, *, created_at, name="Group") -> Group:
    group = Group(id=uuid4(), organization_id=None, name=name, short_code=uuid4().hex[:8], created_at=created_at)
    db_session.add(group)
    await db_session.flush()
    return group


async def _make_admin_user(db_session, group: Group) -> GroupAdmin:
    from app.core.security import hash_password
    from app.modules.auth.models import User, UserRole

    user = User(
        id=uuid4(),
        email=f"{uuid4().hex[:8]}@example.com",
        password_hash=hash_password("P@ssword123"),
        first_name="Rep",
        last_name="Admin",
        role=UserRole.GROUP_ADMIN,
        is_verified=True,
    )
    db_session.add(user)
    await db_session.flush()
    admin = GroupAdmin(id=uuid4(), user_id=user.id, group_id=group.id)
    db_session.add(admin)
    await db_session.flush()
    return admin


async def _make_purse(db_session, group: Group, admin: GroupAdmin, *, created_at) -> Purse:
    purse = Purse(
        id=uuid4(),
        group_id=group.id,
        created_by_group_admin_id=admin.id,
        title="Purse",
        amount=Decimal("1000.00"),
        deadline=_now() + timedelta(days=30),
        enroll_mode=EnrollMode.SNAPSHOT,
        status=PurseStatus.OPEN,
        created_at=created_at,
    )
    db_session.add(purse)
    await db_session.flush()
    return purse


async def _make_contribution(
    db_session,
    purse: Purse,
    admin: GroupAdmin,
    *,
    status: ContributionStatus,
    amount_expected: Decimal = Decimal("1000.00"),
    amount_received: Decimal = Decimal("0"),
    fee_percent_applied=None,
    invoice_id=None,
    paid_at=None,
    created_at=None,
) -> Contribution:
    contribution = Contribution(
        id=uuid4(),
        purse_id=purse.id,
        group_admin_id=admin.id,
        amount_expected=amount_expected,
        amount_received=amount_received,
        platform_fee_percent_applied=fee_percent_applied,
        status=status,
        invoice_id=invoice_id,
        paid_at=paid_at,
        created_at=created_at or _now(),
    )
    db_session.add(contribution)
    await db_session.flush()
    return contribution


async def _write_event(db_session, contribution, *, from_status, to_status, actor_type, created_at):
    event = ContributionEvent(
        id=uuid4(),
        contribution_id=contribution.id,
        from_status=from_status,
        to_status=to_status,
        actor_type=actor_type,
        created_at=created_at,
    )
    db_session.add(event)
    await db_session.flush()
    return event


async def test_overview_computes_fee_revenue_conversion_active_new_retention(client, db_session):
    now = _now()
    headers = await _admin_headers(db_session)

    # Group A: old enough (45 days) to be retention-eligible at days=30,
    # has 2 purses (created 40 and 3 days ago) -> retained. One PAID
    # contribution 10 days ago (amount 1000, fee 2% => fee revenue 20.00),
    # one PAID_MANUAL 5 days ago with no locked-in fee percent (contributes
    # 0 to fee revenue, per the null-handling rule), and one still-PENDING
    # contribution with an invoice (counts in conversion's denominator only).
    group_a = await _make_group(db_session, created_at=now - timedelta(days=45), name="Group A")
    admin_a = await _make_admin_user(db_session, group_a)
    purse_a1 = await _make_purse(db_session, group_a, admin_a, created_at=now - timedelta(days=40))
    purse_a2 = await _make_purse(db_session, group_a, admin_a, created_at=now - timedelta(days=3))
    await _make_contribution(
        db_session, purse_a1, admin_a,
        status=ContributionStatus.PAID, amount_received=Decimal("1000.00"),
        fee_percent_applied=Decimal("2.00"), invoice_id="inv-a1",
        paid_at=now - timedelta(days=10), created_at=now - timedelta(days=10),
    )
    await _make_contribution(
        db_session, purse_a2, admin_a,
        status=ContributionStatus.PAID_MANUAL, amount_received=Decimal("500.00"),
        fee_percent_applied=None, invoice_id=None,
        paid_at=now - timedelta(days=5), created_at=now - timedelta(days=5),
    )
    member_a = Member(id=uuid4(), user_id=admin_a.user_id, group_id=group_a.id)
    db_session.add(member_a)
    await db_session.flush()
    pending_contribution = Contribution(
        id=uuid4(),
        purse_id=purse_a2.id,
        member_id=member_a.id,
        amount_expected=Decimal("1000.00"),
        amount_received=Decimal("0"),
        status=ContributionStatus.PENDING,
        invoice_id="inv-a3",
        created_at=now - timedelta(days=2),
    )
    db_session.add(pending_contribution)
    await db_session.flush()

    # Group B: also 45 days old, retention-eligible, but only 1 purse ->
    # not retained. No activity in the last 30 days -> dormant.
    group_b = await _make_group(db_session, created_at=now - timedelta(days=45), name="Group B")
    admin_b = await _make_admin_user(db_session, group_b)
    await _make_purse(db_session, group_b, admin_b, created_at=now - timedelta(days=45))

    # Group C: created 5 days ago -> counts as new_groups_count, too young
    # for the retention denominator, and active via its purse's created_at.
    group_c = await _make_group(db_session, created_at=now - timedelta(days=5), name="Group C")
    admin_c = await _make_admin_user(db_session, group_c)
    await _make_purse(db_session, group_c, admin_c, created_at=now - timedelta(days=1))

    await db_session.commit()

    resp = await client.get("/admin/analytics/overview?days=30", headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    # fee revenue: 1000 * 2% + 500 * 0% = 20.00
    assert data["total_fee_revenue"] == "20.00"
    # conversion: of {inv-a1 (paid), inv-a3 (pending)} invoiced in-window, 1/2 paid
    assert data["conversion_rate"] == 50.0
    # active: group A (paid contribution + purse), group C (purse) -- group B has neither
    assert data["active_groups_count"] == 2
    assert data["new_groups_count"] == 1
    # retention: eligible = {A, B} (both >30d old), retained = {A} (2 purses) -> 1/2
    assert data["retention_rate"] == 50.0


async def test_revenue_trend_and_growth_trend_bucket_correctly(client, db_session):
    now = _now()
    headers = await _admin_headers(db_session)

    group = await _make_group(db_session, created_at=now - timedelta(days=6), name="Trend Group")
    admin = await _make_admin_user(db_session, group)
    purse1 = await _make_purse(db_session, group, admin, created_at=now - timedelta(days=6))
    purse2 = await _make_purse(db_session, group, admin, created_at=now - timedelta(days=6))
    # Paid 2 days ago (fee 10.00) and paid 6 days ago (fee 5.00) -- with a
    # 7-day window these land in two different daily buckets. Two purses,
    # not one, since a (purse, group_admin) pair can only own one
    # Contribution row (uq_contribution_purse_group_admin).
    await _make_contribution(
        db_session, purse1, admin, status=ContributionStatus.PAID,
        amount_received=Decimal("1000.00"), fee_percent_applied=Decimal("1.00"),
        invoice_id="inv-1", paid_at=now - timedelta(days=2), created_at=now - timedelta(days=2),
    )
    await _make_contribution(
        db_session, purse2, admin, status=ContributionStatus.PAID_MANUAL,
        amount_received=Decimal("500.00"), fee_percent_applied=Decimal("1.00"),
        invoice_id=None, paid_at=now - timedelta(days=6), created_at=now - timedelta(days=6),
    )
    await db_session.commit()

    resp = await client.get("/admin/analytics/revenue-trend?days=7", headers=headers)
    assert resp.status_code == 200, resp.text
    points = resp.json()["data"]
    assert len(points) == 7
    total = sum(Decimal(p["amount"]) for p in points)
    assert total == Decimal("15.00")

    growth_resp = await client.get("/admin/analytics/growth-trend?days=7", headers=headers)
    assert growth_resp.status_code == 200, growth_resp.text
    growth_points = growth_resp.json()["data"]
    assert len(growth_points) == 7
    assert sum(p["new_groups"] for p in growth_points) == 1


async def test_needs_attention_backlog_trend_and_rescue_rate(client, db_session):
    now = _now()
    headers = await _admin_headers(db_session)

    group = await _make_group(db_session, created_at=now - timedelta(days=45), name="Attn Group")
    admin = await _make_admin_user(db_session, group)
    # Three purses, not one -- a (purse, group_admin) pair can only own
    # one Contribution row (uq_contribution_purse_group_admin), and this
    # test needs three independent contributions on the same admin.
    purse1 = await _make_purse(db_session, group, admin, created_at=now - timedelta(days=45))
    purse2 = await _make_purse(db_session, group, admin, created_at=now - timedelta(days=45))
    purse3 = await _make_purse(db_session, group, admin, created_at=now - timedelta(days=45))

    flagged = await _make_contribution(
        db_session, purse1, admin, status=ContributionStatus.FLAGGED_FOR_REVIEW,
        invoice_id="inv-f", created_at=now - timedelta(days=40),
    )
    # 2 new flags in the recent 30d window, 1 in the prior 30d window -> increasing.
    await _write_event(
        db_session, flagged, from_status=ContributionStatus.PENDING,
        to_status=ContributionStatus.FLAGGED_FOR_REVIEW, actor_type=ActorType.WEBHOOK,
        created_at=now - timedelta(days=45),
    )
    await _write_event(
        db_session, flagged, from_status=ContributionStatus.PENDING,
        to_status=ContributionStatus.FLAGGED_FOR_REVIEW, actor_type=ActorType.WEBHOOK,
        created_at=now - timedelta(days=10),
    )
    await _write_event(
        db_session, flagged, from_status=ContributionStatus.PENDING,
        to_status=ContributionStatus.FLAGGED_FOR_REVIEW, actor_type=ActorType.WEBHOOK,
        created_at=now - timedelta(days=5),
    )

    # Rescue rate: 2 paid contributions in the last 30 days, one confirmed
    # by the reconciliation job, one by a webhook -> 50%.
    paid_by_webhook = await _make_contribution(
        db_session, purse2, admin, status=ContributionStatus.PAID,
        amount_received=Decimal("100.00"), invoice_id="inv-w",
        paid_at=now - timedelta(days=3), created_at=now - timedelta(days=3),
    )
    await _write_event(
        db_session, paid_by_webhook, from_status=ContributionStatus.PENDING,
        to_status=ContributionStatus.PAID, actor_type=ActorType.WEBHOOK,
        created_at=now - timedelta(days=3),
    )
    paid_by_reconciliation = await _make_contribution(
        db_session, purse3, admin, status=ContributionStatus.PAID,
        amount_received=Decimal("100.00"), invoice_id="inv-r",
        paid_at=now - timedelta(days=2), created_at=now - timedelta(days=2),
    )
    await _write_event(
        db_session, paid_by_reconciliation, from_status=ContributionStatus.PENDING,
        to_status=ContributionStatus.PAID, actor_type=ActorType.RECONCILIATION_JOB,
        created_at=now - timedelta(days=2),
    )
    await db_session.commit()

    resp = await client.get("/admin/analytics/needs-attention", headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["flagged_backlog_count"] == 1
    assert data["flagged_backlog_trend"] == "increasing"
    assert data["webhook_rescue_rate"] == 50.0


async def test_provider_split_attributes_volume_by_current_settlement_provider(client, db_session):
    now = _now()
    headers = await _admin_headers(db_session)

    group_p = await _make_group(db_session, created_at=now - timedelta(days=45), name="Paystack Group")
    admin_p = await _make_admin_user(db_session, group_p)
    purse_p = await _make_purse(db_session, group_p, admin_p, created_at=now - timedelta(days=45))
    db_session.add(
        SettlementAccount(
            id=uuid4(), group_id=group_p.id, bank_code="044", bank_name="Access",
            account_number="0000000000", settlement_mode=SettlementMode.DIRECT,
            payment_provider="paystack", created_by_group_admin_id=admin_p.id,
        )
    )
    await _make_contribution(
        db_session, purse_p, admin_p, status=ContributionStatus.PAID,
        amount_received=Decimal("1000.00"), invoice_id="inv-p",
        paid_at=now - timedelta(days=1), created_at=now - timedelta(days=1),
    )

    group_m = await _make_group(db_session, created_at=now - timedelta(days=45), name="Monnify Group")
    admin_m = await _make_admin_user(db_session, group_m)
    purse_m = await _make_purse(db_session, group_m, admin_m, created_at=now - timedelta(days=45))
    db_session.add(
        SettlementAccount(
            id=uuid4(), group_id=group_m.id, bank_code="044", bank_name="Access",
            account_number="0000000001", settlement_mode=SettlementMode.DIRECT,
            payment_provider="monnify", created_by_group_admin_id=admin_m.id,
        )
    )
    await _make_contribution(
        db_session, purse_m, admin_m, status=ContributionStatus.PAID,
        amount_received=Decimal("300.00"), invoice_id="inv-m",
        paid_at=now - timedelta(days=1), created_at=now - timedelta(days=1),
    )
    await db_session.commit()

    resp = await client.get("/admin/analytics/provider-split?days=30", headers=headers)
    assert resp.status_code == 200, resp.text
    by_provider = {row["provider"]: row for row in resp.json()["data"]}
    assert set(by_provider) == {"monnify", "paystack"}
    assert by_provider["paystack"]["volume"] == "1000.00"
    assert by_provider["paystack"]["success_rate"] == 100.0
    assert by_provider["monnify"]["volume"] == "300.00"
    assert by_provider["monnify"]["success_rate"] == 100.0


async def test_groups_health_classifies_and_paginates(client, db_session):
    now = _now()
    headers = await _admin_headers(db_session)

    active_group = await _make_group(db_session, created_at=now - timedelta(days=45), name="Active Group")
    admin_active = await _make_admin_user(db_session, active_group)
    await _make_purse(db_session, active_group, admin_active, created_at=now - timedelta(days=5))
    db_session.add(Member(id=uuid4(), user_id=admin_active.user_id, group_id=active_group.id))

    dormant_group = await _make_group(db_session, created_at=now - timedelta(days=45), name="Dormant Group")
    admin_dormant = await _make_admin_user(db_session, dormant_group)
    await _make_purse(db_session, dormant_group, admin_dormant, created_at=now - timedelta(days=45))

    new_group = await _make_group(db_session, created_at=now - timedelta(days=2), name="New Group")
    await db_session.commit()

    active_resp = await client.get("/admin/analytics/groups-health?status=active&days=30", headers=headers)
    assert active_resp.status_code == 200, active_resp.text
    active_body = active_resp.json()["data"]
    active_names = {item["name"] for item in active_body["items"]}
    assert "Active Group" in active_names
    assert "Dormant Group" not in active_names
    active_item = next(i for i in active_body["items"] if i["name"] == "Active Group")
    assert active_item["purse_count"] == 1
    assert active_item["member_count"] == 1
    assert active_item["last_activity_at"] is not None

    dormant_resp = await client.get("/admin/analytics/groups-health?status=dormant&days=30", headers=headers)
    dormant_names = {item["name"] for item in dormant_resp.json()["data"]["items"]}
    assert "Dormant Group" in dormant_names
    assert "Active Group" not in dormant_names

    new_resp = await client.get("/admin/analytics/groups-health?status=new&days=30", headers=headers)
    new_names = {item["name"] for item in new_resp.json()["data"]["items"]}
    assert "New Group" in new_names
    assert "Dormant Group" not in new_names

    paged = await client.get(
        "/admin/analytics/groups-health?status=active&days=30&limit=1&offset=0", headers=headers
    )
    assert paged.json()["data"]["limit"] == 1
    assert paged.json()["data"]["total"] >= 1


async def test_analytics_endpoints_require_platform_admin(client, db_session):
    resp = await client.get("/admin/analytics/overview?days=30")
    assert resp.status_code == 401

    resp = await client.get("/admin/analytics/groups-health?status=active")
    assert resp.status_code == 401


async def test_days_param_rejects_values_outside_the_fixed_set(client, db_session):
    headers = await _admin_headers(db_session)
    resp = await client.get("/admin/analytics/overview?days=14", headers=headers)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "validation_error"
