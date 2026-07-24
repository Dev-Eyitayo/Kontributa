import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

from app.core.config import settings
from app.core.db import Base
from app.modules.auth import models as auth_models  # noqa: F401
from app.modules.organizations import models as organizations_models  # noqa: F401
from app.modules.group_admins import models as group_admins_models  # noqa: F401
from app.modules.invites import models as invites_models  # noqa: F401
from app.modules.members import models as members_models  # noqa: F401
from app.modules.purses import models as purses_models  # noqa: F401
from app.modules.contributions import models as contributions_models  # noqa: F401
from app.modules.webhooks import models as webhooks_models  # noqa: F401
from app.modules.settlement import models as settlement_models  # noqa: F401
from app.modules.payouts import models as payouts_models  # noqa: F401
from app.modules.audit import models as audit_models  # noqa: F401
from app.modules.notifications import models as notifications_models  # noqa: F401
from app.modules.platform_settings import models as platform_settings_models  # noqa: F401

config = context.config
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
