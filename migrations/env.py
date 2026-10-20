import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import get_settings
from app.db.models import Base
from app.db.session import make_engine
from app.db.types import Money

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def render_item(type_: str, obj: object, autogen_context) -> str | bool:  # noqa: ANN001
    # Keep migrations self-contained: Money (a TypeDecorator) is stored as BIGINT cents.
    if type_ == "type" and isinstance(obj, Money):
        return "sa.BigInteger()"
    return False


def _url() -> str:
    # An explicit -x url=... or config option wins (tests); otherwise DATABASE_URL.
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:  # noqa: ANN001
    # render_as_batch: SQLite can't ALTER most things, so Alembic rebuilds tables.
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine: AsyncEngine = make_engine(_url())
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
