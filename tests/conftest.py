import asyncio

import fakeredis.aioredis
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.db import Base, get_db
from app.core.redis import get_redis
from app.core.security import hash_password
from app.main import app

# Ensure every module's models are registered on Base.metadata before create_all.
from app.modules.auth import models as _auth_models  # noqa: F401
from app.modules.auth.models import User
from app.modules.group_admins import models as _group_admin_models  # noqa: F401
from app.modules.invites import models as _invite_models  # noqa: F401
from app.modules.members import models as _member_models  # noqa: F401
from app.modules.organizations import models as _org_models  # noqa: F401
from app.modules.organizations.models import Group, Organization, OrganizationType


def _prepare_schema_once() -> None:
    async def _run():
        engine = create_async_engine(settings.DATABASE_URL, poolclass=pool.NullPool)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_run())


_prepare_schema_once()

_state: dict = {}


@pytest_asyncio.fixture(autouse=True)
async def db_setup():
    # Function-scoped so the async engine and its connections are bound to
    # the same event loop pytest-asyncio creates for this test function.
    engine = create_async_engine(settings.DATABASE_URL, poolclass=pool.NullPool)
    session_local = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    _state["engine"] = engine
    _state["session_local"] = session_local
    _state["redis"] = fake_redis

    async def _override_get_db():
        async with session_local() as session:
            yield session

    async def _override_get_redis():
        return fake_redis

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_redis] = _override_get_redis

    yield

    async with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())
    await fake_redis.flushall()
    await engine.dispose()

    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_redis, None)


@pytest_asyncio.fixture
async def client(db_setup):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def db_session(db_setup):
    async with _state["session_local"]() as session:
        yield session


async def find_redis_token(prefix: str) -> str:
    keys = await _state["redis"].keys(f"{prefix}:*")
    assert keys, f"no redis key found for prefix {prefix}"
    key = keys[0]
    return key.split(":", 1)[1]


async def create_org_and_group(
    db_session,
    org_name: str = "Lead City University",
    org_short_code: str = "LCU",
    group_name: str = "Computer Science",
    group_short_code: str = "CSC",
    member_id_format: str | None = None,
) -> tuple[Organization, Group]:
    org = Organization(
        name=org_name,
        short_code=org_short_code,
        org_type=OrganizationType.SCHOOL,
        member_id_format=member_id_format,
    )
    db_session.add(org)
    await db_session.flush()

    group = Group(organization_id=org.id, name=group_name, short_code=group_short_code)
    db_session.add(group)
    await db_session.commit()
    await db_session.refresh(org)
    await db_session.refresh(group)
    return org, group


async def create_platform_admin(db_session, email: str = "admin@example.com") -> User:
    admin = User(
        email=email,
        password_hash=hash_password("adminpass123"),
        first_name="Platform",
        last_name="Admin",
        role="group_admin",
        is_verified=True,
        is_platform_admin=True,
    )
    db_session.add(admin)
    await db_session.commit()
    await db_session.refresh(admin)
    return admin
