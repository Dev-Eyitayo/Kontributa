"""
One-off cleanup for accounts that ended up holding both an active
GroupAdmin row AND a Member row for the SAME group -- a self-referential
membership that's now blocked going forward (see
MemberService.join_additional_group's admin_cannot_join_own_group check),
but may already exist from before that block was added.

Dry-run by default -- prints a report and changes nothing. Pass --confirm
to actually remove the safe cases (no contribution history at all). A
pair with ANY contribution history is never auto-deleted, no matter which
flag is passed -- those need a human to look at, since real money may
have moved through that Member row. Every row this script actually
removes gets an AuditLog entry (actor: RECONCILIATION_JOB -- the closest
existing "automated, not a human" actor type -- see
app/modules/audit/models.py's AuditActorType), same as every other
money-adjacent state change in this system.

Usage (from the repo root):
    source .venv/bin/activate
    python scripts/cleanup_self_referential_memberships.py              # dry run (default)
    python scripts/cleanup_self_referential_memberships.py --dry-run    # same, explicit
    python scripts/cleanup_self_referential_memberships.py --confirm    # actually remove the safe cases
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.modules.audit.models import AuditActorType
from app.modules.audit.service import AuditService
from app.modules.auth.models import User
from app.modules.contributions.models import Contribution
from app.modules.group_admins.models import GroupAdmin
from app.modules.members.models import Member
from app.modules.organizations.models import Group


class SelfReferentialCase:
    def __init__(self, user: User, group: Group, member: Member, has_history: bool):
        self.user = user
        self.group = group
        self.member = member
        self.has_history = has_history


async def find_self_referential_cases(db) -> list[SelfReferentialCase]:
    result = await db.execute(
        select(Member, GroupAdmin, User, Group)
        .join(
            GroupAdmin,
            (GroupAdmin.user_id == Member.user_id) & (GroupAdmin.group_id == Member.group_id),
        )
        .join(User, User.id == Member.user_id)
        .join(Group, Group.id == Member.group_id)
        .where(Member.removed_at.is_(None), GroupAdmin.is_active_admin.is_(True))
    )

    cases = []
    for member, _admin, user, group in result.all():
        has_history = (
            await db.execute(select(Contribution.id).where(Contribution.member_id == member.id).limit(1))
        ).scalar_one_or_none() is not None
        cases.append(SelfReferentialCase(user=user, group=group, member=member, has_history=has_history))
    return cases


def _print_report(cases: list[SelfReferentialCase]) -> None:
    clean = [c for c in cases if not c.has_history]
    dirty = [c for c in cases if c.has_history]

    print(f"Found {len(cases)} self-referential (admin + member, same group) pair(s).")
    print()

    if clean:
        print(f"Safe to auto-clean ({len(clean)}) -- no contribution history:")
        for c in clean:
            print(
                f"  - user={c.user.email} ({c.user.id})  group={c.group.name} ({c.group.id})  "
                f"member_row={c.member.id}  action=remove Member row"
            )
        print()

    if dirty:
        print(f"NEEDS MANUAL REVIEW -- do not auto-clean ({len(dirty)}) -- has contribution history:")
        for c in dirty:
            print(
                f"  - user={c.user.email} ({c.user.id})  group={c.group.name} ({c.group.id})  "
                f"member_row={c.member.id}  action=NONE (manual review required)"
            )
        print()


def _member_before_state(member: Member) -> dict:
    return {
        "id": str(member.id),
        "user_id": str(member.user_id),
        "group_id": str(member.group_id),
        "cohort": member.cohort,
        "member_id_number": member.member_id_number,
        "verification_status": member.verification_status.value,
        "invite_source": str(member.invite_source) if member.invite_source else None,
        "created_at": member.created_at.isoformat(),
    }


async def clean_up(db, cases: list[SelfReferentialCase], confirm: bool) -> int:
    if not confirm:
        return 0

    audit = AuditService(db)
    removed = 0
    for c in cases:
        if c.has_history:
            continue
        before_state = _member_before_state(c.member)
        await audit.record_event(
            entity_type="member",
            entity_id=c.member.id,
            action="self_referential_membership_removed",
            actor_type=AuditActorType.RECONCILIATION_JOB,
            actor_id=None,
            before_state=before_state,
            after_state=None,
        )
        await db.delete(c.member)
        removed += 1
    await db.commit()
    return removed


async def main(confirm: bool) -> None:
    engine = create_async_engine(settings.DATABASE_URL)
    session_local = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with session_local() as db:
        cases = await find_self_referential_cases(db)
        _print_report(cases)

        if not cases:
            await engine.dispose()
            print("Nothing to do.")
            return

        clean_count = sum(1 for c in cases if not c.has_history)
        dirty_count = len(cases) - clean_count

        removed = 0
        if confirm:
            removed = await clean_up(db, cases, confirm=True)
            print(f"Removed {removed} self-referential Member row(s), each with an AuditLog entry.")
        else:
            print(f"Dry run -- no changes made. Re-run with --confirm to remove the {clean_count} safe case(s).")

        print()
        print("Summary:")
        print(f"  found:               {len(cases)}")
        print(f"  auto-cleaned:        {removed}")
        print(f"  needs manual review: {dirty_count}")

    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually remove the safe (no contribution history) cases. Omit for a dry run (default).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicit dry run -- same as omitting --confirm. Provided for clarity in scripts/CI.",
    )
    args = parser.parse_args()
    if args.dry_run and args.confirm:
        raise SystemExit("--dry-run and --confirm are mutually exclusive")
    asyncio.run(main(confirm=args.confirm))
