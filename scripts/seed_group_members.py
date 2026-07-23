"""
Populates a Group with a batch of fake Members, one Purse (contribution
plan) for that group, and a Contribution row per member -- some already
marked paid -- so there's realistic-looking data to click through locally.

Writes directly via SQLAlchemy rather than through the API/invite flow,
since the point here is bulk fixture data, not exercising the join path.
Safe to re-run -- looks each row up by its unique field first, so running
it again just tops up whatever counts you pass.

Usage (from the repo root):
    source .venv/bin/activate
    python scripts/seed_group_members.py
    python scripts/seed_group_members.py --members 80 --paid 20
"""
import argparse
import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.auth import UserRole
from app.core.config import settings
from app.core.security import hash_password
from app.modules.auth.models import User
from app.modules.contributions.models import Contribution, ContributionStatus
from app.modules.group_admins.models import GroupAdmin
from app.modules.invites.models import InviteLink  # noqa: F401 -- registers invite_links for Member's FK
from app.modules.members.models import Member, VerificationStatus
from app.modules.organizations.models import Group, Organization, OrganizationType
from app.modules.purses.models import EnrollMode, Purse, PurseStatus

ORG_NAME = "Lead City University"
ORG_SHORT_CODE = "LCU"

GROUP_NAME = "Software Engineering"
GROUP_SHORT_CODE = "SE"

ADMIN_EMAIL = "rep-se@kontributa.app"
ADMIN_PASSWORD = "AdminPass123!"

MEMBER_EMAIL_TEMPLATE = "se-member-{n:02d}@kontributa.app"
MEMBER_PASSWORD = "MemberPass123!"

PURSE_TITLE = "Departmental Dues"
PURSE_AMOUNT = Decimal("5000.00")

DEFAULT_MEMBER_COUNT = 55
DEFAULT_PAID_COUNT = 10


async def get_or_create_org(db) -> Organization:
    result = await db.execute(select(Organization).where(Organization.short_code == ORG_SHORT_CODE))
    org = result.scalar_one_or_none()
    if org is None:
        org = Organization(
            id=uuid.uuid4(), name=ORG_NAME, short_code=ORG_SHORT_CODE, org_type=OrganizationType.SCHOOL
        )
        db.add(org)
        await db.flush()
        print(f"created organization: {ORG_NAME} ({ORG_SHORT_CODE})")
    return org


async def get_or_create_group(db, org: Organization) -> Group:
    result = await db.execute(
        select(Group).where(Group.organization_id == org.id, Group.short_code == GROUP_SHORT_CODE)
    )
    group = result.scalar_one_or_none()
    if group is None:
        group = Group(id=uuid.uuid4(), organization_id=org.id, name=GROUP_NAME, short_code=GROUP_SHORT_CODE)
        db.add(group)
        await db.flush()
        print(f"created group: {GROUP_NAME} ({GROUP_SHORT_CODE})")
    return group


async def get_or_create_group_admin(db, group: Group) -> GroupAdmin:
    result = await db.execute(select(User).where(User.email == ADMIN_EMAIL))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(
            id=uuid.uuid4(),
            email=ADMIN_EMAIL,
            password_hash=hash_password(ADMIN_PASSWORD),
            first_name="Group",
            last_name="Admin",
            role=UserRole.GROUP_ADMIN,
            is_verified=True,
        )
        db.add(user)
        await db.flush()

    result = await db.execute(
        select(GroupAdmin).where(GroupAdmin.user_id == user.id, GroupAdmin.group_id == group.id)
    )
    admin = result.scalar_one_or_none()
    if admin is None:
        admin = GroupAdmin(id=uuid.uuid4(), user_id=user.id, group_id=group.id, is_active_admin=True)
        db.add(admin)
        await db.flush()
        print(f"created group admin: {ADMIN_EMAIL}")
    return admin


async def get_or_create_members(db, group: Group, count: int) -> list[Member]:
    members = []
    for n in range(1, count + 1):
        email = MEMBER_EMAIL_TEMPLATE.format(n=n)
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(
                id=uuid.uuid4(),
                email=email,
                password_hash=hash_password(MEMBER_PASSWORD),
                first_name="Member",
                last_name=f"{n:02d}",
                role=UserRole.MEMBER,
                is_verified=True,
            )
            db.add(user)
            await db.flush()

        result = await db.execute(select(Member).where(Member.user_id == user.id, Member.group_id == group.id))
        member = result.scalar_one_or_none()
        if member is None:
            member = Member(
                id=uuid.uuid4(),
                user_id=user.id,
                group_id=group.id,
                member_id_number=f"{GROUP_SHORT_CODE}/2024/{n:04d}",
                verification_status=VerificationStatus.VERIFIED,
            )
            db.add(member)
            await db.flush()
        members.append(member)
    print(f"members ready: {len(members)}")
    return members


async def get_or_create_purse(db, group: Group, admin: GroupAdmin) -> Purse:
    result = await db.execute(select(Purse).where(Purse.group_id == group.id, Purse.title == PURSE_TITLE))
    purse = result.scalar_one_or_none()
    if purse is None:
        purse = Purse(
            id=uuid.uuid4(),
            group_id=group.id,
            created_by_group_admin_id=admin.id,
            title=PURSE_TITLE,
            amount=PURSE_AMOUNT,
            deadline=datetime.now(timezone.utc) + timedelta(days=30),
            enroll_mode=EnrollMode.SNAPSHOT,
            status=PurseStatus.OPEN,
        )
        db.add(purse)
        await db.flush()
        print(f"created purse: {PURSE_TITLE} ({PURSE_AMOUNT})")
    return purse


async def get_or_create_contributions(db, purse: Purse, members: list[Member], paid_count: int) -> None:
    paid = 0
    created = 0
    for member in members:
        result = await db.execute(
            select(Contribution).where(Contribution.purse_id == purse.id, Contribution.member_id == member.id)
        )
        contribution = result.scalar_one_or_none()
        if contribution is not None:
            if contribution.status in (ContributionStatus.PAID, ContributionStatus.PAID_MANUAL):
                paid += 1
            continue

        is_paid = paid < paid_count
        contribution = Contribution(
            id=uuid.uuid4(),
            purse_id=purse.id,
            member_id=member.id,
            amount_expected=purse.amount,
            amount_received=purse.amount if is_paid else Decimal("0"),
            status=ContributionStatus.PAID_MANUAL if is_paid else ContributionStatus.PENDING,
            paid_at=datetime.now(timezone.utc) if is_paid else None,
        )
        db.add(contribution)
        created += 1
        if is_paid:
            paid += 1
    print(f"contributions created this run: {created} (paid so far: {paid}/{len(members)})")


async def main(member_count: int, paid_count: int) -> None:
    if paid_count > member_count:
        raise SystemExit("--paid cannot exceed --members")

    engine = create_async_engine(settings.DATABASE_URL)
    session_local = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with session_local() as db:
        org = await get_or_create_org(db)
        group = await get_or_create_group(db, org)
        admin = await get_or_create_group_admin(db, group)
        members = await get_or_create_members(db, group, member_count)
        purse = await get_or_create_purse(db, group, admin)
        await get_or_create_contributions(db, purse, members, paid_count)
        await db.commit()

    await engine.dispose()

    print()
    print("Done.")
    print(f"Organization: {ORG_NAME} ({ORG_SHORT_CODE})")
    print(f"Group:        {GROUP_NAME} ({GROUP_SHORT_CODE})")
    print(f"Purse:        {PURSE_TITLE} -- {PURSE_AMOUNT} x {member_count} members, {paid_count} already paid")
    print()
    print(f"Group admin login: {ADMIN_EMAIL} / {ADMIN_PASSWORD}")
    print(f"Any member login:  {MEMBER_EMAIL_TEMPLATE.format(n=1)} / {MEMBER_PASSWORD}")
    print("(member emails run se-member-01 .. se-member-{:02d}, all sharing the same password)".format(member_count))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--members", type=int, default=DEFAULT_MEMBER_COUNT, help="how many members to seed")
    parser.add_argument("--paid", type=int, default=DEFAULT_PAID_COUNT, help="how many of them to mark as paid")
    args = parser.parse_args()
    asyncio.run(main(args.members, args.paid))
