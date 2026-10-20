from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import Chat, Message, Update, User

from app.bank.mock import MockBankProvider
from app.bot.setup import make_bot, make_dispatcher
from app.companies import load_companies_file, seed_approvers, seed_companies
from app.config import Settings
from app.db.session import Database, create_schema

ROOT = Path(__file__).resolve().parent.parent
APPROVER_ID = 1001
STRANGER_ID = 6666


class FakeTelegramSession(BaseSession):
    """Records every Bot API call instead of sending it; answers sendMessage with a Message."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,  # noqa: ASYNC109 (aiogram signature)
    ) -> TelegramType:
        self.requests.append(method)
        chat_id = getattr(method, "chat_id", 0)
        return Message(  # type: ignore[return-value]
            message_id=len(self.requests),
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type="private"),
            text=getattr(method, "text", None),
        )

    async def stream_content(self, *args: Any, **kwargs: Any):  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:
        pass

    @property
    def texts(self) -> list[str]:
        return [m.text for m in self.requests if hasattr(m, "text")]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        companies_file=ROOT / "config" / "companies.example.yaml",
        bootstrap_approver_ids=[APPROVER_ID],
        telegram_bot_token="42:TEST",
    )


@pytest.fixture
async def db(settings: Settings) -> AsyncIterator[Database]:
    database = Database(settings.database_url)
    await create_schema(database.engine)
    async with database.session() as s:
        await seed_companies(s, load_companies_file(settings.companies_file))
        await seed_approvers(s, settings.bootstrap_approver_ids)
    yield database
    await database.dispose()


@pytest.fixture
def bank(db: Database) -> MockBankProvider:
    return MockBankProvider(db)


class BotHarness:
    def __init__(self, settings: Settings, db: Database, bank: MockBankProvider) -> None:
        self.session = FakeTelegramSession()
        self.bot = make_bot("42:TEST", session=self.session)
        self.dp = make_dispatcher(settings, db, bank)
        self._update_id = 0

    async def send(self, text: str, user_id: int = APPROVER_ID) -> list[str]:
        """Feed a private text message through the dispatcher; return the bot's replies."""
        before = len(self.session.requests)
        self._update_id += 1
        user = User(id=user_id, is_bot=False, first_name="Tester")
        msg = Message(
            message_id=self._update_id,
            date=datetime.now(UTC),
            chat=Chat(id=user_id, type="private"),
            from_user=user,
            text=text,
            entities=_command_entities(text),
        )
        await self.dp.feed_update(self.bot, Update(update_id=self._update_id, message=msg))
        return [m.text for m in self.session.requests[before:] if hasattr(m, "text")]


def _command_entities(text: str) -> list[dict[str, Any]] | None:
    if not text.startswith("/"):
        return None
    return [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]


@pytest.fixture
def harness(settings: Settings, db: Database, bank: MockBankProvider) -> BotHarness:
    return BotHarness(settings, db, bank)
