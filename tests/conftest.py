import asyncio

import fakeredis.aioredis
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import pool, text
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
from app.modules.notifications.service import SendByteError, get_sendbyte_client

# Ensure every module's models are registered on Base.metadata before create_all.
from app.modules.audit import models as _audit_models  # noqa: F401
from app.modules.auth import models as _auth_models  # noqa: F401
from app.modules.auth.models import User
from app.modules.contributions import models as _contribution_models  # noqa: F401
from app.modules.group_admins import models as _group_admin_models  # noqa: F401
from app.modules.invites import models as _invite_models  # noqa: F401
from app.modules.members import models as _member_models  # noqa: F401
from app.modules.notifications import models as _notifications_models  # noqa: F401
from app.modules.organizations import models as _org_models  # noqa: F401
from app.modules.organizations.models import Group, Organization, OrganizationType
from app.modules.payouts import models as _payout_models  # noqa: F401
from app.modules.purses import models as _purse_models  # noqa: F401
from app.modules.settlement import models as _settlement_models  # noqa: F401
from app.modules.webhooks import models as _webhook_models  # noqa: F401


def _prepare_schema_once() -> None:
    async def _run():
        # Tests build the schema directly from Base.metadata rather than
        # running Alembic migrations, so the Phase 6 migration's role
        # creation + GRANT/REVOKE setup (see 83d4db43c592) has to be
        # mirrored here too -- otherwise the REVOKE-permissions test would
        # be exercising a role that was never actually restricted.
        engine = create_async_engine(settings.TEST_DATABASE_URL, poolclass=pool.NullPool)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

            role = settings.APP_DB_ROLE
            password = settings.APP_DB_PASSWORD
            db_name = engine.url.database
            superuser = engine.url.username

            await conn.execute(
                text(
                    f"""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') THEN
                            CREATE ROLE {role} LOGIN PASSWORD '{password}';
                        END IF;
                    END
                    $$;
                    """
                )
            )
            await conn.execute(text(f"GRANT CONNECT ON DATABASE {db_name} TO {role}"))
            await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
            await conn.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}"))
            await conn.execute(text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}"))
            await conn.execute(
                text(
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {superuser} IN SCHEMA public "
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}"
                )
            )
            for audit_table in ("audit_log", "contribution_events", "payout_events"):
                await conn.execute(text(f"REVOKE UPDATE, DELETE ON {audit_table} FROM {role}"))

            await conn.execute(text("INSERT INTO audit_chain_head (id, last_row_hash) VALUES (1, NULL)"))
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

    async def list_banks(self) -> list[dict]:
        return [{"bank_code": "058", "bank_name": "Guaranty Trust Bank"}, {"bank_code": "011", "bank_name": "First Bank"}]

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


class FakeSendByteClient:
    """Stands in for the real SendByte API in tests -- no network access."""

    def __init__(self):
        self.sent: list[dict] = []
        self.should_fail = False

    async def send(self, to_email: str, to_name: str, subject: str, html: str) -> str:
        if self.should_fail:
            raise SendByteError("simulated SendByte send failure")
        self.sent.append({"to_email": to_email, "to_name": to_name, "subject": subject, "html": html})
        return f"fake-message-{len(self.sent)}"


@pytest_asyncio.fixture(autouse=True)
async def db_setup():
    # Function-scoped so the async engine and its connections are bound to
    # the same event loop pytest-asyncio creates for this test function.
    #
    # The app-facing engine connects as the restricted runtime role (same
    # as production), so every test genuinely exercises the app under the
    # role that has UPDATE/DELETE revoked on the audit tables -- not just
    # the schema-bootstrap-time superuser connection. Teardown's wipe-all
    # step needs the superuser instead, since it deletes from those same
    # audit tables between tests, which the restricted role can't do.
    engine = create_async_engine(settings.TEST_RUNTIME_DATABASE_URL, poolclass=pool.NullPool)
    session_local = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    admin_engine = create_async_engine(settings.TEST_DATABASE_URL, poolclass=pool.NullPool)
    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    fake_monnify = FakeMonnifyClient()
    fake_sendbyte = FakeSendByteClient()

    _state["engine"] = engine
    _state["session_local"] = session_local
    _state["redis"] = fake_redis
    _state["monnify"] = fake_monnify
    _state["sendbyte"] = fake_sendbyte

    async def _override_get_db():
        async with session_local() as session:
            yield session

    async def _override_get_redis():
        return fake_redis

    def _override_get_monnify_client():
        return fake_monnify

    def _override_get_sendbyte_client():
        return fake_sendbyte

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_redis] = _override_get_redis
    app.dependency_overrides[get_monnify_client] = _override_get_monnify_client
    app.dependency_overrides[get_sendbyte_client] = _override_get_sendbyte_client

    yield

    async with admin_engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())
        # audit_chain_head is infrastructure, not test data -- reseed its
        # single row so the next test's record_event() calls have a head
        # row to lock, in sync with audit_log having just been wiped too.
        await conn.execute(text("INSERT INTO audit_chain_head (id, last_row_hash) VALUES (1, NULL)"))
    await fake_redis.flushall()
    await engine.dispose()
    await admin_engine.dispose()

    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_redis, None)
    app.dependency_overrides.pop(get_monnify_client, None)
    app.dependency_overrides.pop(get_sendbyte_client, None)


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
