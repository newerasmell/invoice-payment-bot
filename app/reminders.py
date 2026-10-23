"""Reminders (M6): issued invoices past their due date that aren't paid, and payment
instructions approved internally that the bank still hasn't shown after
INSTRUCTION_TIMEOUT_HOURS. Each item is repeated at most every REMINDER_REPEAT_HOURS."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import (
    Company,
    Counterparty,
    InstructionStatus,
    Invoice,
    InvoiceStatus,
    PaymentInstruction,
)
from app.db.session import Database

log = logging.getLogger(__name__)


@dataclass
class Reminder:
    kind: str  # invoice_overdue | instruction_waiting
    company_id: int
    entity_id: int
    text: str


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


def _due_again(last: datetime | None, now: datetime, settings: Settings) -> bool:
    last = _aware(last)
    return last is None or now - last >= timedelta(hours=settings.reminder_repeat_hours)


async def collect(
    session: AsyncSession, settings: Settings, now: datetime | None = None
) -> list[Reminder]:
    """Find what needs a reminder now and mark it reminded (the caller commits)."""
    from app.bot import texts  # avoid an import cycle at module load

    now = now or datetime.now(UTC)
    today: date = now.date()
    out: list[Reminder] = []
    rows = (
        await session.execute(
            select(Invoice, Company, Counterparty)
            .join(Company, Company.id == Invoice.issuer_id)
            .join(Counterparty, Counterparty.id == Invoice.counterparty_id)
            .where(
                Invoice.status.in_((InvoiceStatus.APPROVED, InvoiceStatus.SENT)),
                Invoice.due_date < today,
            )
            .order_by(Invoice.due_date, Invoice.id)
        )
    ).all()
    for inv, issuer, client in rows:
        if not _due_again(inv.reminded_at, now, settings):
            continue
        inv.reminded_at = now
        days = (today - inv.due_date).days
        out.append(
            Reminder(
                "invoice_overdue",
                issuer.id,
                inv.id,
                texts.reminder_invoice(inv, issuer, client, days),
            )
        )
    cutoff = now - timedelta(hours=settings.instruction_timeout_hours)
    instrs = (
        await session.execute(
            select(PaymentInstruction, Company)
            .join(Company, Company.id == PaymentInstruction.from_company_id)
            .where(PaymentInstruction.status == InstructionStatus.AWAITING_BANK)
            .order_by(PaymentInstruction.id)
        )
    ).all()
    for instr, payer in instrs:
        approved = _aware(instr.approved_at)
        if approved is None or approved > cutoff:
            continue
        if not _due_again(instr.reminded_at, now, settings):
            continue
        instr.reminded_at = now
        hours = int((now - approved).total_seconds() // 3600)
        out.append(
            Reminder(
                "instruction_waiting",
                payer.id,
                instr.id,
                texts.reminder_instruction(instr, payer, hours),
            )
        )
    return out


async def send(bot: Bot, db: Database, reminders: list[Reminder]) -> None:
    from app.bot.documents_flow import Ins
    from app.bot.payments_flow import _recipients

    for r in reminders:
        async with db.session() as s:
            company = await s.get(Company, r.company_id)
        kb = None
        if r.kind == "instruction_waiting":
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=f"Show #{r.entity_id}",
                            callback_data=Ins(action="show", id=r.entity_id).pack(),
                        )
                    ]
                ]
            )
        for chat in await _recipients(db, company):
            try:
                await bot.send_message(chat, r.text, reply_markup=kb)
            except Exception:
                log.warning("could not send a reminder to %s", chat, exc_info=True)


async def run_once(settings: Settings, db: Database, bot: Bot | None) -> list[Reminder]:
    async with db.session() as s:
        reminders = await collect(s, settings)
        await s.commit()
    if bot is not None and reminders:
        await send(bot, db, reminders)
    return reminders


async def maybe_weekly_digest(
    settings: Settings, db: Database, bot: Bot | None, now: datetime | None = None
) -> bool:
    """Send the weekly summary once per ISO week, at/after the configured local day and hour."""
    from zoneinfo import ZoneInfo

    from sqlalchemy import func

    from app.audit import audit
    from app.bot.report_flow import send_weekly_digest
    from app.db.models import AuditLog

    if bot is None or not settings.weekly_digest_enabled:
        return False
    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:  # no tz database on the host: fall back to UTC
        tz = UTC
    local = (now or datetime.now(UTC)).astimezone(tz)
    if local.weekday() != settings.digest_weekday or local.hour < settings.digest_hour:
        return False
    week = f"{local.isocalendar().year}-W{local.isocalendar().week:02d}"
    async with db.session() as s:
        done = await s.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.action == "digest.sent", AuditLog.entity == week
            )
        )
        if done:
            return False
        audit(s, "system", "digest.sent", week)  # claim the week first: never send twice
        await s.commit()
    sent = await send_weekly_digest(bot, db, settings, local.date())
    log.info("weekly digest %s: %d message(s)", week, sent)
    return True


async def remind_forever(settings: Settings, db: Database, bot: Bot | None) -> None:
    await asyncio.sleep(30)  # let the app finish starting
    while True:
        try:
            sent = await run_once(settings, db, bot)
            if sent:
                log.info("reminders: %d sent", len(sent))
            await maybe_weekly_digest(settings, db, bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("reminders failed")
        await asyncio.sleep(settings.reminder_check_seconds)
