"""
Seeds the minimum reference data needed to start using the API locally:
a platform admin user, one Organization, and one Group under it.

Safe to re-run -- looks each row up by its unique field first and only
inserts what's missing, so running this against a DB that already has
some of this data just fills in the rest.

Usage (from the repo root):
    source .venv/bin/activate
    python scripts/seed.py
"""
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.security import hash_password
from app.modules.auth.models import User
from app.modules.organizations.models import Group, Organization, OrganizationType

ADMIN_EMAIL = "admin@kontributa.app"
ADMIN_PASSWORD = "AdminPass123!"

ORG_NAME = "Lead City University"
ORG_SHORT_CODE = "LCU"

GROUP_NAME = "Software Engineering"
GROUP_SHORT_CODE = "SE"


async def main() -> None:
    engine = create_async_engine(settings.DATABASE_URL)
    session_local = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with session_local() as db:
        # --- Platform admin ---------------------------------------------
        result = await db.execute(select(User).where(User.email == ADMIN_EMAIL))
        admin = result.scalar_one_or_none()
        if admin is None:
            admin = User(
                id=uuid.uuid4(),
                email=ADMIN_EMAIL,
                password_hash=hash_password(ADMIN_PASSWORD),
                first_name="Platform",
                last_name="Admin",
                role="group_admin",
                is_verified=True,
                is_platform_admin=True,
            )
            db.add(admin)
            print(f"created platform admin: {ADMIN_EMAIL}")
        else:
            changed = False
            if not admin.is_platform_admin:
                admin.is_platform_admin = True
                changed = True
            if not admin.is_verified:
                admin.is_verified = True
                changed = True
            print(f"platform admin already exists: {ADMIN_EMAIL}" + (" (promoted/verified)" if changed else ""))

        # --- Organization -------------------------------------------------
        result = await db.execute(select(Organization).where(Organization.short_code == ORG_SHORT_CODE))
        org = result.scalar_one_or_none()
        if org is None:
            org = Organization(
                id=uuid.uuid4(),
                name=ORG_NAME,
                short_code=ORG_SHORT_CODE,
                org_type=OrganizationType.SCHOOL,
                member_id_format=None,
            )
            db.add(org)
            await db.flush()
            print(f"created organization: {ORG_NAME} ({ORG_SHORT_CODE})")
        else:
            print(f"organization already exists: {ORG_NAME} ({ORG_SHORT_CODE})")

        # --- Group ----------------------------------------------------------
        result = await db.execute(
            select(Group).where(Group.organization_id == org.id, Group.short_code == GROUP_SHORT_CODE)
        )
        group = result.scalar_one_or_none()
        if group is None:
            group = Group(
                id=uuid.uuid4(),
                organization_id=org.id,
                name=GROUP_NAME,
                short_code=GROUP_SHORT_CODE,
            )
            db.add(group)
            print(f"created group: {GROUP_NAME} ({GROUP_SHORT_CODE}) under {ORG_SHORT_CODE}")
        else:
            print(f"group already exists: {GROUP_NAME} ({GROUP_SHORT_CODE}) under {ORG_SHORT_CODE}")

        await db.commit()

    await engine.dispose()

    print()
    print("Done. Platform admin login:")
    print(f"  email:    {ADMIN_EMAIL}")
    print(f"  password: {ADMIN_PASSWORD}")
    print(f"Organization: {ORG_NAME} ({ORG_SHORT_CODE})")
    print(f"Group:        {GROUP_NAME} ({GROUP_SHORT_CODE})")
    print()
    print("Change the admin password after first login -- this is a seed default, not production-safe.")


if __name__ == "__main__":
    asyncio.run(main())
