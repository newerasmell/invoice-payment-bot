from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode

from app.bank.base import BankProvider
from app.bot.auth import WhitelistMiddleware
from app.bot.handlers import make_routers
from app.config import Settings
from app.db.session import Database


def make_dispatcher(settings: Settings, db: Database, bank: BankProvider) -> Dispatcher:
    dp = Dispatcher(settings=settings, db=db, bank=bank)
    dp.update.outer_middleware(WhitelistMiddleware(db))
    dp.include_routers(*make_routers(settings.dev_commands_enabled))
    return dp


def make_bot(token: str, session: BaseSession | None = None) -> Bot:
    return Bot(token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
