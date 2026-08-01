"""Exercises scripts/cleanup_self_referential_memberships.py directly
(importing its functions rather than shelling out) against cases that
predate the admin_cannot_join_own_group block -- simulated here by
inserting the self-referential Member row straight via the DB session,
since the API itself now refuses to create one."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select

from app.modules.audit.models import AuditLog
from app.modules.contributions.models import Contribution
from app.modules.group_admins.models import GroupAdmin
from app.modules.members.models import Member, VerificationStatus
from app.modules.purses.models import EnrollMode, Purse, PurseStatus
from scripts.cleanup_self_referential_memberships import clean_up, find_self_referential_cases
from tests.conftest import create_org_and_group, find_redis_token, onboard_group_admin


async def _register_verify_login(client, email: str, password: str = "P@ssword123") -> tuple[dict, str]:
    reg = await client.post(
        "/auth/register",
        json={
            "email": email,
            "password": password,
            "first_name": "Test",
            "last_name": "Admin",
            "role": "group_admin",
        },
    )
    user_id = reg.json()["data"]["id"]
    verify_token = await find_redis_token("verify_email")
    await client.post("/auth/verify-email", json={"email": email, "token": verify_token})
    login = await client.post("/auth/login", json={"email": email, "password": password})
    token = login.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}, user_id


async def _insert_self_referential_member(db_session, user_id: str, group, cohort=None) -> Member:
    """Simulates data that predates the block -- a Member row for the same
    (user, group) an active GroupAdmin row already covers. Bypasses the
    API on purpose, since MemberService.join_additional_group now rejects
    this outright."""
    member = Member(
        id=uuid4(),
        user_id=UUID(user_id),
        group_id=group.id,
        cohort=cohort,
        verification_status=VerificationStatus.VERIFIED,
    )
    db_session.add(member)
    await db_session.commit()
    await db_session.refresh(member)
    return member


async def _add_contribution(db_session, group, member: Member) -> Contribution:
    purse = Purse(
        id=uuid4(),
        group_id=group.id,
        created_by_group_admin_id=(
            await db_session.execute(select(GroupAdmin).where(GroupAdmin.group_id == group.id))
        )
        .scalars()
        .first()
        .id,
        title="Test Purse",
        amount=Decimal("1000.00"),
        deadline=datetime.now(timezone.utc) + timedelta(days=30),
        enroll_mode=EnrollMode.SNAPSHOT,
        status=PurseStatus.OPEN,
    )
    db_session.add(purse)
    await db_session.flush()

    contribution = Contribution(
        id=uuid4(),
        purse_id=purse.id,
        member_id=member.id,
        amount_expected=purse.amount,
        amount_received=Decimal("0"),
    )
    db_session.add(contribution)
    await db_session.commit()
    return contribution


async def test_finds_and_categorizes_clean_vs_dirty_self_referential_cases(client, db_session):
    org, _existing_group = await create_org_and_group(db_session)

    clean_headers, clean_user_id = await _register_verify_login(client, "clean-self-admin@example.com")
    clean_group = await onboard_group_admin(client, db_session, org, clean_headers, group_name="Clean Self Group")
    clean_member = await _insert_self_referential_member(db_session, clean_user_id, clean_group)

    dirty_headers, dirty_user_id = await _register_verify_login(client, "dirty-self-admin@example.com")
    dirty_group = await onboard_group_admin(client, db_session, org, dirty_headers, group_name="Dirty Self Group")
    dirty_member = await _insert_self_referential_member(db_session, dirty_user_id, dirty_group)
    await _add_contribution(db_session, dirty_group, dirty_member)

    cases = await find_self_referential_cases(db_session)
    found_member_ids = {c.member.id for c in cases}
    assert clean_member.id in found_member_ids
    assert dirty_member.id in found_member_ids

    by_id = {c.member.id: c for c in cases}
    assert by_id[clean_member.id].has_history is False
    assert by_id[dirty_member.id].has_history is True


async def test_confirm_removes_only_the_clean_case_and_writes_an_audit_log_entry(client, db_session):
    org, _existing_group = await create_org_and_group(db_session)

    clean_headers, clean_user_id = await _register_verify_login(client, "confirm-clean-admin@example.com")
    clean_group = await onboard_group_admin(client, db_session, org, clean_headers, group_name="Confirm Clean Group")
    clean_member = await _insert_self_referential_member(db_session, clean_user_id, clean_group)

    dirty_headers, dirty_user_id = await _register_verify_login(client, "confirm-dirty-admin@example.com")
    dirty_group = await onboard_group_admin(client, db_session, org, dirty_headers, group_name="Confirm Dirty Group")
    dirty_member = await _insert_self_referential_member(db_session, dirty_user_id, dirty_group)
    await _add_contribution(db_session, dirty_group, dirty_member)

    cases = await find_self_referential_cases(db_session)
    relevant = [c for c in cases if c.member.id in {clean_member.id, dirty_member.id}]
    assert len(relevant) == 2

    removed = await clean_up(db_session, relevant, confirm=True)
    assert removed == 1

    remaining_clean = await db_session.get(Member, clean_member.id)
    assert remaining_clean is None

    remaining_dirty = await db_session.get(Member, dirty_member.id)
    assert remaining_dirty is not None

    audit_entries = (
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.entity_type == "member",
                    AuditLog.entity_id == clean_member.id,
                    AuditLog.action == "self_referential_membership_removed",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(audit_entries) == 1
    assert audit_entries[0].before_state["id"] == str(clean_member.id)
    assert audit_entries[0].after_state is None

    dirty_audit_entries = (
        (await db_session.execute(select(AuditLog).where(AuditLog.entity_id == dirty_member.id)))
        .scalars()
        .all()
    )
    assert dirty_audit_entries == []


async def test_dry_run_reports_but_changes_nothing(client, db_session):
    org, _existing_group = await create_org_and_group(db_session)
    headers, user_id = await _register_verify_login(client, "dry-run-admin@example.com")
    group = await onboard_group_admin(client, db_session, org, headers, group_name="Dry Run Group")
    member = await _insert_self_referential_member(db_session, user_id, group)

    cases = await find_self_referential_cases(db_session)
    relevant = [c for c in cases if c.member.id == member.id]
    assert len(relevant) == 1

    removed = await clean_up(db_session, relevant, confirm=False)
    assert removed == 0

    still_there = await db_session.get(Member, member.id)
    assert still_there is not None
