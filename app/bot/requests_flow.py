"""Client invoice requests in Telegram: the notification, Create draft / Decline, /requests."""

import logging
from html import escape

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.bot import texts
from app.bot.approvals import can_act_for
from app.config import Settings
from app.db.models import AllowedUser, Company, Counterparty, InvoiceRequest, RequestStatus
from app.db.session import Database
from app.invoices import requests as reqs
from app.invoices import service as svc

log = logging.getLogger(__name__)


class Req(CallbackData, prefix="rq"):
    action: str  # dr (create draft) | no (decline)
    id: int


def request_kb(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.BTN_REQUEST_DRAFT, callback_data=Req(action="dr", id=req_id).pack()
                ),
                InlineKeyboardButton(
                    text=texts.BTN_REQUEST_DECLINE, callback_data=Req(action="no", id=req_id).pack()
                ),
            ]
        ]
    )


async def _load(s, req_id: int):  # noqa: ANN001, ANN202
    req = await s.get(InvoiceRequest, req_id)
    if req is None:
        return None, None, None
    return req, await s.get(Company, req.company_id), await s.get(Counterparty, req.counterparty_id)


async def notify_request(bot: Bot, db: Database, req_id: int) -> None:
    from app.bot.payments_flow import _recipients

    async with db.session() as s:
        req, company, client = await _load(s, req_id)
        text = texts.request_card(req, company, client, reqs.total(req.lines))
    for chat in await _recipients(db, company):
        try:
            await bot.send_message(chat, text, reply_markup=request_kb(req_id))
        except Exception:
            log.warning("could not notify %s about request %s", chat, req_id, exc_info=True)


async def on_request(
    cb: CallbackQuery,
    callback_data: Req,
    bot: Bot,
    db: Database,
    settings: Settings,
    allowed_user: AllowedUser,
) -> None:
    from app.bot.invoice_flow import send_draft_preview  # avoid an import cycle

    async with db.session() as s:
        req, company, client = await _load(s, callback_data.id)
        if req is None:
            await cb.answer("Request not found", show_alert=True)
            return
        if not can_act_for(allowed_user, company):
            await cb.answer(texts.NOT_YOUR_COMPANY, show_alert=True)
            return
        try:
            if callback_data.action == "dr":
                inv = await reqs.to_draft(s, settings, req, allowed_user.telegram_id)
                inv_id = inv.id
            else:
                await reqs.decline(s, req, allowed_user.telegram_id)
                inv_id = None
            await s.commit()
        except (reqs.RequestError, svc.InvoiceError) as e:
            await cb.answer()
            await cb.message.answer(f"❗ {escape(str(e))}")
            return
        card = texts.request_card(req, company, client, reqs.total(req.lines))
    await cb.answer()
    await cb.message.edit_text(card, reply_markup=None)
    if inv_id is not None:
        await send_draft_preview(
            bot, cb.message.chat.id, db, inv_id, header=texts.REQUEST_DRAFT_HEADER
        )
    else:
        await cb.message.answer(texts.REQUEST_DECLINED)


async def cmd_requests(message: Message, db: Database, allowed_user: AllowedUser) -> None:
    async with db.session() as s:
        rows = (
            await s.execute(
                select(InvoiceRequest, Company, Counterparty)
                .join(Company, Company.id == InvoiceRequest.company_id)
                .join(Counterparty, Counterparty.id == InvoiceRequest.counterparty_id)
                .where(InvoiceRequest.status == RequestStatus.NEW)
                .order_by(InvoiceRequest.id)
            )
        ).all()
    rows = [r for r in rows if can_act_for(allowed_user, r[1])]
    if not rows:
        await message.answer(texts.NO_REQUESTS)
        return
    for req, company, client in rows[:10]:
        await message.answer(
            texts.request_card(req, company, client, reqs.total(req.lines)),
            reply_markup=request_kb(req.id),
        )


def make_router() -> Router:
    r = Router(name="requests")
    r.message.register(cmd_requests, Command("requests"))
    r.callback_query.register(on_request, Req.filter())
    return r
