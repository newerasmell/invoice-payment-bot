from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.models import Base


def make_engine(url: str) -> AsyncEngine:
    if url.startswith("sqlite") and ":///" in url and ":memory:" not in url:
        Path(url.split(":///", 1)[1]).parent.mkdir(parents=True, exist_ok=True)
    sqlite = url.startswith("sqlite")
    # Writers wait up to 30 s for the lock instead of failing (e.g. two approvals at once).
    engine = create_async_engine(url, connect_args={"timeout": 30} if sqlite else {})
    if sqlite:

        @event.listens_for(engine.sync_engine, "connect")
        def _fk_on(dbapi_conn, _):  # noqa: ANN001
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
            if ":memory:" not in url:
                # WAL lets readers (admin pages, public links) work while a write is open.
                dbapi_conn.execute("PRAGMA journal_mode=WAL")

    return engine


async def create_schema(engine: AsyncEngine) -> None:
    """Test helper only. The app itself builds the schema with Alembic migrations."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


class Database:
    def __init__(self, url: str) -> None:
        self.engine = make_engine(url)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as s:
            yield s

    async def dispose(self) -> None:
        await self.engine.dispose()
