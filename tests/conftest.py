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
from app.modules.payments.schemas import (
    MonnifyAccountName,
    MonnifyInvoice,
    MonnifyTransactionStatus,
    MonnifyTransferResult,
)
from app.modules.payments.service import MonnifyError, get_monnify_client

# Ensure every module's models are registered on Base.metadata before create_all.
from app.modules.auth import models as _auth_models  # noqa: F401
from app.modules.auth.models import User
from app.modules.contributions import models as _contribution_models  # noqa: F401
from app.modules.group_admins import models as _group_admin_models  # noqa: F401
from app.modules.invites import models as _invite_models  # noqa: F401
from app.modules.members import models as _member_models  # noqa: F401
from app.modules.organizations import models as _org_models  # noqa: F401
from app.modules.organizations.models import Group, Organization, OrganizationType
from app.modules.payouts import models as _payout_models  # noqa: F401
from app.modules.purses import models as _purse_models  # noqa: F401
from app.modules.settlement import models as _settlement_models  # noqa: F401
from app.modules.webhooks import models as _webhook_models  # noqa: F401


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


class FakeMonnifyClient:
    """Stands in for the real Monnify API in tests -- no network access."""

    def __init__(self):
        self.created_invoices: list[str] = []
        self.transaction_statuses: dict[str, MonnifyTransactionStatus] = {}
        self.account_names: dict[str, str] = {}
        self.bank_names: dict[str, str] = {}
        self.transfers: list[dict] = []
        self.transfer_should_fail = False

    async def create_invoice(
        self, invoice_reference, amount, customer_name, customer_email, description, expires_at
    ) -> MonnifyInvoice:
        self.created_invoices.append(invoice_reference)
        return MonnifyInvoice(
            invoice_reference=invoice_reference,
            account_number=f"90{len(self.created_invoices):08d}",
            bank_name="Test Bank",
            account_name=customer_name,
            amount=amount,
            expires_at=expires_at,
        )

    async def get_transaction_status(self, payment_reference: str) -> MonnifyTransactionStatus:
        if payment_reference in self.transaction_statuses:
            return self.transaction_statuses[payment_reference]
        return MonnifyTransactionStatus(
            transaction_reference="",
            payment_reference=payment_reference,
            payment_status="PENDING",
            amount_paid=0,
            paid_on=None,
        )

    async def verify_account_name(self, account_number: str, bank_code: str) -> MonnifyAccountName:
        resolved_name = self.account_names.get(account_number, "Default Resolved Name")
        return MonnifyAccountName(account_number=account_number, bank_code=bank_code, account_name=resolved_name)

    async def get_bank_name(self, bank_code: str) -> str:
        return self.bank_names.get(bank_code, "Test Bank")

    async def initiate_single_transfer(
        self, reference, amount, bank_code, account_number, account_name, narration
    ) -> MonnifyTransferResult:
        self.transfers.append(
            {
                "reference": reference,
                "amount": amount,
                "bank_code": bank_code,
                "account_number": account_number,
                "account_name": account_name,
            }
        )
        if self.transfer_should_fail:
            raise MonnifyError("simulated transfer initiation failure")
        return MonnifyTransferResult(reference=reference, status="PENDING")


@pytest_asyncio.fixture(autouse=True)
async def db_setup():
    # Function-scoped so the async engine and its connections are bound to
    # the same event loop pytest-asyncio creates for this test function.
    engine = create_async_engine(settings.DATABASE_URL, poolclass=pool.NullPool)
    session_local = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    fake_monnify = FakeMonnifyClient()

    _state["engine"] = engine
    _state["session_local"] = session_local
    _state["redis"] = fake_redis
    _state["monnify"] = fake_monnify

    async def _override_get_db():
        async with session_local() as session:
            yield session

    async def _override_get_redis():
        return fake_redis

    def _override_get_monnify_client():
        return fake_monnify

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_redis] = _override_get_redis
    app.dependency_overrides[get_monnify_client] = _override_get_monnify_client

    yield

    async with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())
    await fake_redis.flushall()
    await engine.dispose()

    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_redis, None)
    app.dependency_overrides.pop(get_monnify_client, None)


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
