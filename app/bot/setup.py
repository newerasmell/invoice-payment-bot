from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from app.bank.base import BankProvider
from app.bot.auth import WhitelistMiddleware
from app.bot.handlers import make_routers
from app.config import Settings
from app.db.session import Database
from app.llm.parser import InvoiceParser

BOT_COMMANDS = [
    BotCommand(command="invoices", description="Recent invoices"),
    BotCommand(command="payments", description="Recent bank transfers"),
    BotCommand(command="instructions", description="Payments we have to make"),
    BotCommand(command="rules", description="Automatic invoicing rules"),
    BotCommand(command="companies", description="Company profiles"),
    BotCommand(command="balance", description="Account balance: /balance <company>"),
    BotCommand(command="credit", description="Credit note for an issued invoice"),
    BotCommand(command="cancel", description="Cancel the current edit"),
    BotCommand(command="help", description="How to create an invoice"),
]


def make_dispatcher(
    settings: Settings, db: Database, bank: BankProvider, parser: InvoiceParser | None = None
) -> Dispatcher:
    if parser is None:
        key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
        parser = InvoiceParser(key, settings.claude_model)
    dp = Dispatcher(settings=settings, db=db, bank=bank, parser=parser)
    dp.update.outer_middleware(WhitelistMiddleware(db))
    dp.include_routers(*make_routers(settings.dev_commands_enabled))
    return dp


def make_bot(token: str, session: BaseSession | None = None) -> Bot:
    return Bot(token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
