from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app.bank.mock import MockBankProvider
from app.bot.setup import make_bot, make_dispatcher
from app.companies import load_companies_file, seed_approvers, seed_companies
from app.config import Settings
from app.db.migrate import upgrade_to_head
from app.db.session import Database

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
        if type(method).__name__ == "GetFile":
            from aiogram.types import File

            return File(
                file_id=method.file_id,
                file_unique_id=method.file_id,  # type: ignore[return-value]
                file_path=f"docs/{method.file_id}",
            )
        chat_id = getattr(method, "chat_id", 0)
        return Message(  # type: ignore[return-value]
            message_id=len(self.requests),
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type="private"),
            text=getattr(method, "text", None),
        ).as_(bot)

    files: dict[str, bytes] = {}

    async def stream_content(self, url: str, *args: Any, **kwargs: Any):  # noqa: ANN201
        yield self.files[url.rsplit("/", 1)[-1]]

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
        uploads_dir=tmp_path / "uploads",
        public_base_url="https://bot.example.com",
    )


@pytest.fixture
async def db(settings: Settings) -> AsyncIterator[Database]:
    database = Database(settings.database_url)
    await upgrade_to_head(settings.database_url)
    async with database.session() as s:
        await seed_companies(s, load_companies_file(settings.companies_file))
        await seed_approvers(s, settings.bootstrap_approver_ids)
    yield database
    await database.dispose()


@pytest.fixture
def bank(db: Database) -> MockBankProvider:
    return MockBankProvider(db)


# Everything an invoice needs to know about a client (see invoices/completeness.py).
CLIENT_DETAILS = {
    "eik": "123456789",
    "address": "10 Customer Ave",
    "city": "Plovdiv",
    "country": "Bulgaria",
}


class FakeParser:
    """Stands in for Claude: returns queued drafts and records what it was asked."""

    available = True

    def __init__(self) -> None:
        self.queue: list = []
        self.party_queue: list = []
        self.doc_queue: list = []
        self.calls: list = []

    async def parse(self, text, ctx, attachments=()):  # noqa: ANN001, ANN201
        self.calls.append((text, ctx, attachments))
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def read_document(self, text, attachments):  # noqa: ANN001, ANN201
        self.calls.append(("doc", text, attachments))
        return self.doc_queue.pop(0)

    async def extract_party(self, text, known, wanted, attachments=()):  # noqa: ANN001, ANN201
        self.calls.append(("party", text, wanted, attachments))
        return self.party_queue.pop(0)


class BotHarness:
    def __init__(self, settings: Settings, db: Database, bank: MockBankProvider) -> None:
        self.session = FakeTelegramSession()
        self.bot = make_bot("42:TEST", session=self.session)
        self.parser = FakeParser()
        self.dp = make_dispatcher(settings, db, bank, parser=self.parser)
        self._update_id = 0

    def calls_since(self, before: int) -> list:
        return self.session.requests[before:]

    async def click(self, data: str, user_id: int = APPROVER_ID, message_id: int = 1) -> list:
        """Press an inline button; returns every Bot API call it caused."""
        before = len(self.session.requests)
        self._update_id += 1
        user = User(id=user_id, is_bot=False, first_name="Tester")
        msg = Message(
            message_id=message_id,
            date=datetime.now(UTC),
            chat=Chat(id=user_id, type="private"),
            text="(button message)",
        )
        cb = CallbackQuery(
            id=str(self._update_id), from_user=user, chat_instance="ci", data=data, message=msg
        )
        await self.dp.feed_update(self.bot, Update(update_id=self._update_id, callback_query=cb))
        return self.session.requests[before:]

    async def send(self, text: str, user_id: int = APPROVER_ID) -> list[str]:
        """Feed a private text message through the dispatcher; return the bot's text replies."""
        await self.send_raw(text, user_id)
        return [m.text for m in self.session.requests[self._before :] if hasattr(m, "text")]

    async def send_raw(
        self,
        text: str,
        user_id: int = APPROVER_ID,
        *,
        document: tuple[str, str, bytes] | None = None,
        photo: bytes | None = None,
    ) -> list:
        """Text message, or a document (filename, mime, bytes) / photo with `text` as caption."""
        before = self._before = len(self.session.requests)
        self._update_id += 1
        user = User(id=user_id, is_bot=False, first_name="Tester")
        extra: dict[str, Any] = {"text": text, "entities": _command_entities(text)}
        if document or photo is not None:
            fid = f"file{self._update_id}"
            self.session.files[fid] = document[2] if document else photo
            extra = {"caption": text or None}
            if document:
                extra["document"] = {
                    "file_id": fid,
                    "file_unique_id": fid,
                    "file_name": document[0],
                    "mime_type": document[1],
                    "file_size": len(document[2]),
                }
            else:
                extra["photo"] = [
                    {
                        "file_id": fid,
                        "file_unique_id": fid,
                        "width": 800,
                        "height": 600,
                        "file_size": len(photo),
                    }
                ]
        msg = Message(
            message_id=self._update_id,
            date=datetime.now(UTC),
            chat=Chat(id=user_id, type="private"),
            from_user=user,
            **extra,
        )
        await self.dp.feed_update(self.bot, Update(update_id=self._update_id, message=msg))
        return self.session.requests[before:]


def _command_entities(text: str) -> list[dict[str, Any]] | None:
    if not text.startswith("/"):
        return None
    return [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]


@pytest.fixture
def harness(settings: Settings, db: Database, bank: MockBankProvider) -> BotHarness:
    return BotHarness(settings, db, bank)


@pytest.fixture
def admin_settings_factory(settings: Settings):  # noqa: ANN201
    """App settings for TestClient-based tests (no Telegram, no background polling)."""
    s = settings.model_copy(
        update={"telegram_bot_token": None, "poll_enabled": False, "reminders_enabled": False}
    )
    return s, s.database_url
