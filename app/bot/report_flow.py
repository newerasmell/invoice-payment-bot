"""/report in Telegram and the weekly digest: a short summary of app.reports with a link to
Admin → Finance."""

import logging
import re
from datetime import date

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy import select

from app import reports, suggestions
from app.bot import texts
from app.config import Settings
from app.db.models import AllowedUser, Company, UserRole
from app.db.session import Database

log = logging.getLogger(__name__)


def period_from_arg(arg: str, today: date) -> reports.Period | None:
    arg = arg.strip().lower()
    presets = {"": "this_month", "last": "last_month", "30": "last_30", "year": "this_year"}
    if arg in presets:
        return reports.period_for(presets[arg], None, None, today)
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", arg)
    if m and 1 <= int(m.group(2)) <= 12:
        start = date(int(m.group(1)), int(m.group(2)), 1)
        nxt = date(start.year + start.month // 12, start.month % 12 + 1, 1)
        return reports.Period(start, min(date.fromordinal(nxt.toordinal() - 1), today))
    return None


async def build_report(
    db: Database, settings: Settings, user: AllowedUser, period: reports.Period
) -> list[str]:
    """One message for all companies, or one per company for a user limited to some."""
    async with db.session() as s:
        if user.companies:
            codes = {c.upper() for c in user.companies}
            scopes = [
                (c.code, c.id)
                for c in (await s.scalars(select(Company).order_by(Company.code))).all()
                if c.code in codes
            ]
        else:
            scopes = [("All companies", None)]
        out = []
        for title, company_id in scopes:
            _, totals = await reports.transfers(s, period, company_id)
            link = None
            if settings.public_base_url:
                link = (
                    f"{settings.public_base_url.rstrip('/')}/admin/finance?"
                    f"from={period.start.isoformat()}&to={period.end.isoformat()}"
                    + (f"&company={company_id}" if company_id else "")
                )
            out.append(
                texts.report(
                    title,
                    period,
                    totals,
                    await reports.receivables(s, period, company_id),
                    await reports.open_invoices(s, date.today(), company_id),
                    await reports.payables(s, company_id),
                    await suggestions.build(s, date.today(), company_id),
                    link,
                )
            )
    return out


async def cmd_report(
    message: Message,
    command: CommandObject,
    db: Database,
    settings: Settings,
    allowed_user: AllowedUser,
) -> None:
    period = period_from_arg(command.args or "", date.today())
    if period is None:
        await message.answer(texts.REPORT_USAGE)
        return
    for text in await build_report(db, settings, allowed_user, period):
        await message.answer(text, disable_web_page_preview=True)


async def send_weekly_digest(bot: Bot, db: Database, settings: Settings, today: date) -> int:
    """Last 7 days + what is owed, to every approver. Returns how many messages were sent."""
    period = reports.Period(date.fromordinal(today.toordinal() - 7), today)
    async with db.session() as s:
        users = (
            await s.scalars(select(AllowedUser).where(AllowedUser.role == UserRole.APPROVER))
        ).all()
    sent = 0
    for u in users:
        for text in await build_report(db, settings, u, period):
            try:
                await bot.send_message(
                    u.telegram_id, "🗓 Weekly summary\n" + text, disable_web_page_preview=True
                )
                sent += 1
            except Exception:
                log.warning("weekly digest to %s failed", u.telegram_id, exc_info=True)
    return sent


def make_router() -> Router:
    r = Router(name="report")
    r.message.register(cmd_report, Command("report"))
    return r
