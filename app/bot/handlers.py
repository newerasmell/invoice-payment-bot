import logging
import shlex
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from app.audit import actor_tg, audit
from app.bank.base import BankProvider
from app.bank.mock import MockBankProvider
from app.bot import (
    documents_flow,
    invoice_flow,
    payments_flow,
    report_flow,
    requests_flow,
    texts,
)
from app.companies import default_bank_account, find_company, list_companies
from app.config import Settings
from app.db.models import AllowedUser, Company, TransferDirection
from app.db.session import Database
from app.money import InvalidAmount, to_money
from app.payments import sync as psync

log = logging.getLogger(__name__)


async def cmd_start(message: Message, allowed_user: AllowedUser) -> None:
    name = escape(message.from_user.first_name if message.from_user else "there")
    await message.answer(texts.START.format(name=name, role=allowed_user.role.value))


async def cmd_help(message: Message, settings: Settings) -> None:
    await message.answer(texts.HELP + (texts.HELP_DEV if settings.dev_commands_enabled else ""))


async def cmd_companies(message: Message, db: Database) -> None:
    async with db.session() as s:
        items = await list_companies(s)
    await message.answer(texts.companies(items) if items else texts.NO_COMPANIES)


async def _resolve_company(message: Message, db: Database, key: str) -> Company | None:
    async with db.session() as s:
        company = await find_company(s, key)
        if company is None:
            known = ", ".join(c.code for c in await list_companies(s)) or "none"
            await message.answer(texts.COMPANY_NOT_FOUND.format(key=escape(key), known=known))
    return company


async def cmd_balance(
    message: Message, command: CommandObject, db: Database, bank: BankProvider
) -> None:
    if not command.args:
        await message.answer(texts.BALANCE_USAGE)
        return
    company = await _resolve_company(message, db, command.args)
    if company is None:
        return
    account = default_bank_account(company)
    if account is None or not account.bank_account_ref:
        await message.answer(texts.NO_BANK_ACCOUNT.format(name=escape(company.name)))
        return
    try:
        bal = await bank.get_balance(account.bank_account_ref)
    except Exception as e:  # surface bank errors to the operator instead of failing silently
        log.exception("balance lookup failed")
        await message.answer(texts.BANK_ERROR.format(error=escape(str(e))))
        return
    await message.answer(texts.balance(company, bal))


# --- dev commands (mock bank only) ------------------------------------------------


async def _simulate(
    message: Message,
    command: CommandObject,
    db: Database,
    bank: MockBankProvider,
    allowed_user: AllowedUser,
    direction: TransferDirection,
    settings: Settings,
) -> None:
    incoming = direction == TransferDirection.INCOMING
    usage = texts.DEV_USAGE_IN if incoming else texts.DEV_USAGE_OUT
    try:
        args = shlex.split(command.args or "")
    except ValueError as e:
        await message.answer(texts.BAD_ARGS.format(error=escape(str(e))))
        return
    if len(args) < 3:
        await message.answer(usage)
        return
    company_key, amount_raw, counterparty, *rest = args
    try:
        amount = to_money(amount_raw)
    except InvalidAmount:
        await message.answer(texts.BAD_AMOUNT.format(value=escape(amount_raw)))
        return
    if amount <= 0:
        await message.answer(texts.BAD_AMOUNT.format(value=escape(amount_raw)))
        return
    counterparty_account: str | None = None
    if not incoming and rest:
        counterparty_account, *rest = rest
    reference = " ".join(rest)

    company = await _resolve_company(message, db, company_key)
    if company is None:
        return
    account = default_bank_account(company)
    if account is None or not account.bank_account_ref:
        await message.answer(texts.NO_BANK_ACCOUNT.format(name=escape(company.name)))
        return
    transfer = await bank.simulate(
        direction=direction,
        account=account.bank_account_ref,
        account_holder=company.legal_name or company.name,
        amount=amount,
        currency=account.currency,
        counterparty_name=counterparty,
        counterparty_account=counterparty_account,
        reference=reference,
    )
    async with db.session() as s:
        audit(
            s,
            actor_tg(allowed_user.telegram_id),
            f"dev.simulate_{direction.value}",
            f"mock_transfer:{transfer.external_id}",
            {"company": company.code, "amount": str(amount), "reference": reference},
        )
        await s.commit()
    await message.answer(texts.simulated(transfer))
    # Same path as a real webhook: re-fetch from the bank, match, notify.
    events = await psync.sync_one(db, bank, transfer.external_id, settings)
    await payments_flow.notify(message.bot, db, events)


async def cmd_dev_in(
    message: Message,
    command: CommandObject,
    db: Database,
    bank: MockBankProvider,
    allowed_user: AllowedUser,
    settings: Settings,
) -> None:
    await _simulate(message, command, db, bank, allowed_user, TransferDirection.INCOMING, settings)


async def cmd_dev_out(
    message: Message,
    command: CommandObject,
    db: Database,
    bank: MockBankProvider,
    allowed_user: AllowedUser,
    settings: Settings,
) -> None:
    await _simulate(message, command, db, bank, allowed_user, TransferDirection.OUTGOING, settings)


async def fallback(message: Message) -> None:
    await message.answer("I didn't understand that. Send /help for the list of commands.")


def make_routers(dev_commands: bool) -> list[Router]:
    """Fresh routers per dispatcher (aiogram routers can only be attached once)."""
    main = Router(name="main")
    main.message.register(cmd_start, CommandStart())
    main.message.register(cmd_help, Command("help"))
    main.message.register(cmd_companies, Command("companies"))
    main.message.register(cmd_balance, Command("balance"))
    fallback_router = Router(name="fallback")
    fallback_router.message.register(fallback, F.text)
    routers = [
        main,
        payments_flow.make_router(),
        requests_flow.make_router(),
        report_flow.make_router(),
        documents_flow.make_router(),
        invoice_flow.make_router(),
        fallback_router,
    ]
    if not dev_commands:
        return routers
    dev = Router(name="dev")
    dev.message.register(cmd_dev_in, Command("dev_simulate_incoming"))
    dev.message.register(cmd_dev_out, Command("dev_simulate_outgoing"))
    return [dev, *routers]  # dev first so the invoice text handler doesn't swallow it
