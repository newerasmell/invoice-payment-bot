"""Payment notifications and the human decision for unclear matches (Flow B)."""

import logging
from datetime import UTC, datetime, timedelta

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.audit import actor_tg, audit
from app.bank.base import BankError, BankProvider
from app.bot import texts
from app.config import Settings
from app.db.models import (
    AllowedUser,
    BankTransfer,
    Company,
    Invoice,
    InvoiceStatus,
    PaymentMatch,
    UserRole,
)
from app.db.session import Database
from app.payments import sync as psync

log = logging.getLogger(__name__)


class Pay(CallbackData, prefix="py"):
    action: str  # ok | no
    t: int  # BankTransfer.id
    i: int = 0  # Invoice.id


def review_kb(transfer_id: int, candidates: list) -> InlineKeyboardMarkup:  # noqa: ANN001
    rows = [
        [
            InlineKeyboardButton(
                text=f"✅ {c.invoice.number} · {c.client.name[:22]} · "
                f"{c.invoice.amount_gross:,.2f}",
                callback_data=Pay(action="ok", t=transfer_id, i=c.invoice.id).pack(),
            )
        ]
        for c in candidates[:5]
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text="Not an invoice payment", callback_data=Pay(action="no", t=transfer_id).pack()
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _recipients(db: Database, company: Company | None) -> list[int]:
    async with db.session() as s:
        users = (await s.scalars(select(AllowedUser))).all()
    return [
        u.telegram_id
        for u in users
        if not u.companies or (company and company.code in {c.upper() for c in u.companies})
    ]


async def notify(bot: Bot | None, db: Database, events: list[psync.PaymentEvent]) -> None:
    if bot is None:
        return
    for ev in events:
        async with db.session() as s:
            bt, company = await psync.load_for_message(s, ev)
            if ev.kind == "paid":
                text, kb = texts.payment_paid(bt, company, ev.result.invoice), None
            else:
                text = texts.payment_review(bt, company, ev.result.method, ev.result.invoice)
                kb = review_kb(bt.id, ev.candidates)
        for chat in await _recipients(db, company):
            try:
                await bot.send_message(chat, text, reply_markup=kb)
            except Exception:  # a user who blocked the bot must not stop the others
                log.warning("could not notify %s", chat, exc_info=True)


async def on_pay(
    cb: CallbackQuery, callback_data: Pay, db: Database, allowed_user: AllowedUser
) -> None:
    if allowed_user.role != UserRole.APPROVER:
        await cb.answer(texts.ONLY_APPROVERS_PAY, show_alert=True)
        return
    actor = actor_tg(allowed_user.telegram_id)
    async with db.session() as s:
        bt = await s.get(BankTransfer, callback_data.t)
        match = await s.scalar(
            select(PaymentMatch).where(PaymentMatch.transfer_id == callback_data.t)
        )
        if bt is None or match is None:
            await cb.answer("Transfer not found", show_alert=True)
            return
        if match.invoice_id is not None or match.method == "dismissed":
            await cb.answer("Already resolved", show_alert=True)
            await cb.message.edit_reply_markup(reply_markup=None)
            return
        if callback_data.action == "no":
            match.method, match.confirmed_by_user = "dismissed", allowed_user.telegram_id
            audit(s, actor, "transfer.dismissed", f"transfer:{bt.id}")
            await s.commit()
            await cb.answer()
            await cb.message.edit_reply_markup(reply_markup=None)
            await cb.message.reply(texts.NOT_A_PAYMENT)
            return
        inv = await s.get(Invoice, callback_data.i)
        if inv is None or inv.status not in (InvoiceStatus.APPROVED, InvoiceStatus.SENT):
            await cb.answer("That invoice is not open any more", show_alert=True)
            return
        match.invoice_id, match.confirmed_by_user = inv.id, allowed_user.telegram_id
        await psync.mark_paid(s, inv, bt, actor, f"confirmed ({match.method})")
        await s.commit()
        number = inv.number or ""
    await cb.answer("Marked as paid")
    await cb.message.edit_reply_markup(reply_markup=None)
    name = cb.from_user.first_name if cb.from_user else "you"
    await cb.message.reply(texts.payment_confirmed(number, name))


async def cmd_payments(
    message: Message, db: Database, bank: BankProvider, settings: Settings, bot: Bot
) -> None:
    # Pull fresh data first so /payments is current even between polling rounds.
    try:
        events = await psync.sync(
            db, bank, datetime.now(UTC) - timedelta(hours=settings.poll_lookback_hours)
        )
        await notify(bot, db, events)
    except BankError as e:
        await message.answer(f"❗ {texts.escape(str(e))}")
    async with db.session() as s:
        rows = (
            await s.execute(
                select(
                    BankTransfer, Company.code, Invoice.number, PaymentMatch.id, PaymentMatch.method
                )
                .outerjoin(Company, Company.id == BankTransfer.company_id)
                .outerjoin(PaymentMatch, PaymentMatch.transfer_id == BankTransfer.id)
                .outerjoin(Invoice, Invoice.id == PaymentMatch.invoice_id)
                .order_by(BankTransfer.booked_at.desc().nulls_last(), BankTransfer.id.desc())
                .limit(15)
            )
        ).all()
    if not rows:
        await message.answer(texts.NO_TRANSFERS)
        return
    view = [
        (bt, code, number, bool(mid) and number is None and method != "dismissed")
        for bt, code, number, mid, method in rows
    ]
    await message.answer(texts.transfers_list(view))


def make_router() -> Router:
    r = Router(name="payments")
    r.message.register(cmd_payments, Command("payments"))
    r.callback_query.register(on_pay, Pay.filter())
    return r
