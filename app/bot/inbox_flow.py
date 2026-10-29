"""Supplier invoices received by email (Flow D) in Telegram: the notice with the attachment,
Read & prepare payment / Ignore, and /inbox."""

import asyncio
import logging
from html import escape
from pathlib import Path

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select

from app.audit import actor_tg, audit
from app.bot import texts
from app.bot.approvals import can_act_for
from app.config import Settings
from app.db.models import AllowedUser, Company, InboxMessage, InboxStatus
from app.db.session import Database
from app.inbox.service import known_sender, pending
from app.llm.parser import Attachment, InvoiceParser, ParserError
from app.payments import instructions as ins

log = logging.getLogger(__name__)


class Mail(CallbackData, prefix="ml"):
    action: str  # rd (read & prepare payment) | ig (ignore)
    id: int


def mail_kb(msg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.BTN_MAIL_READ, callback_data=Mail(action="rd", id=msg_id).pack()
                ),
                InlineKeyboardButton(
                    text=texts.BTN_MAIL_IGNORE, callback_data=Mail(action="ig", id=msg_id).pack()
                ),
            ]
        ]
    )


async def _card(db: Database, msg_id: int) -> tuple[InboxMessage, Company, str]:
    async with db.session() as s:
        m = await s.get(InboxMessage, msg_id)
        company = await s.get(Company, m.company_id)
        text = texts.mail_card(m, company, await known_sender(s, m.sender or ""))
    return m, company, text


async def notify_mail(bot: Bot, db: Database, msg_id: int) -> None:
    from app.bot.payments_flow import _recipients

    m, company, text = await _card(db, msg_id)
    first = m.attachments[0]
    data = await asyncio.to_thread(Path(first["path"]).read_bytes)
    for chat in await _recipients(db, company):
        try:
            await bot.send_document(
                chat,
                BufferedInputFile(data, filename=first["name"]),
                caption=text,
                reply_markup=mail_kb(msg_id),
            )
        except Exception:
            log.warning("could not send inbox notice %s to %s", msg_id, chat, exc_info=True)


async def on_mail(
    cb: CallbackQuery,
    callback_data: Mail,
    db: Database,
    parser: InvoiceParser,
    settings: Settings,
    allowed_user: AllowedUser,
) -> None:
    from app.bot.documents_flow import card_kb

    async with db.session() as s:
        m = await s.get(InboxMessage, callback_data.id)
        company = await s.get(Company, m.company_id) if m else None
        if m is None or company is None:
            await cb.answer("Email not found", show_alert=True)
            return
        if not can_act_for(allowed_user, company):
            await cb.answer(texts.NOT_YOUR_COMPANY, show_alert=True)
            return
        if m.status not in (InboxStatus.NEW, InboxStatus.FAILED):
            await cb.answer(f"Already {m.status.value.lower()}", show_alert=True)
            return
        if callback_data.action == "ig":
            m.status, m.decided_by = InboxStatus.IGNORED, allowed_user.telegram_id
            audit(s, actor_tg(allowed_user.telegram_id), "inbox.ignored", f"inbox:{m.id}")
            await s.commit()
            await cb.answer("Ignored")
            await cb.message.edit_reply_markup(reply_markup=None)
            return
        files = m.attachments
    if not parser.available:
        await cb.answer(texts.PARSER_DISABLED, show_alert=True)
        return
    await cb.answer()
    status = await cb.message.answer(texts.READING_DOC)
    attachments = tuple(
        [
            Attachment(f["media_type"], await asyncio.to_thread(Path(f["path"]).read_bytes))
            for f in files[:3]
        ]
    )
    try:
        doc = await parser.read_document(
            f"Invoice received by email in the mailbox of {company.code}; "
            "it is an invoice that company has to pay.",
            attachments,
        )
    except ParserError as e:
        await _failed(db, callback_data.id, allowed_user, str(e))
        await status.edit_text(f"❗ {escape(str(e))}")
        return
    async with db.session() as s:
        m = await s.get(InboxMessage, callback_data.id)
        company = await s.get(Company, m.company_id)
        try:
            instr = await ins.create_from_document(
                s,
                settings,
                payer=company,
                doc=doc,
                document_path=files[0]["path"],
                created_by=allowed_user.telegram_id,
            )
        except ins.InstructionError as e:
            await s.rollback()
            await _failed(db, callback_data.id, allowed_user, str(e))
            await status.edit_text(f"❗ {escape(str(e))}")
            return
        if m.auth != "pass":
            instr.note = "\n".join(filter(None, [instr.note, texts.MAIL_UNVERIFIED_NOTE]))
        m.status, m.instruction_id = InboxStatus.PREPARED, instr.id
        m.decided_by = allowed_user.telegram_id
        audit(
            s,
            actor_tg(allowed_user.telegram_id),
            "inbox.prepared",
            f"inbox:{m.id}",
            {"instruction": instr.id, "auth": m.auth},
        )
        await s.commit()
        card, kb = texts.instruction_card(instr, company), card_kb(instr)
    await status.delete()
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(card, reply_markup=kb)


async def _failed(db: Database, msg_id: int, user: AllowedUser, detail: str) -> None:
    async with db.session() as s:
        m = await s.get(InboxMessage, msg_id)
        m.status, m.detail, m.decided_by = InboxStatus.FAILED, detail[:500], user.telegram_id
        await s.commit()


async def cmd_inbox(message: Message, db: Database, allowed_user: AllowedUser) -> None:
    async with db.session() as s:
        companies = (await s.scalars(select(Company))).all()
        ids = [c.id for c in companies if can_act_for(allowed_user, c)]
        rows = await pending(s, ids)
        cards = []
        for m in rows[:10]:
            company = next(c for c in companies if c.id == m.company_id)
            cards.append((m.id, texts.mail_card(m, company, await known_sender(s, m.sender or ""))))
    if not cards:
        await message.answer(texts.NO_MAIL)
        return
    for msg_id, text in cards:
        await message.answer(text, reply_markup=mail_kb(msg_id))


def make_router() -> Router:
    r = Router(name="inbox")
    r.message.register(cmd_inbox, Command("inbox"))
    r.callback_query.register(on_mail, Mail.filter())
    return r
