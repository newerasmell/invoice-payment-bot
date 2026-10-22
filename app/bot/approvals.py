"""Two-person approval above the amount limits (M6): who is asked for the second approval."""

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select

from app.db.models import AllowedUser, Company, UserRole
from app.db.session import Database

log = logging.getLogger(__name__)


def user_label(u: AllowedUser) -> str:
    return u.display_name or f"user {u.telegram_id}"


def can_act_for(u: AllowedUser, company: Company) -> bool:
    return not u.companies or company.code in {c.upper() for c in u.companies}


async def approvers_for(db: Database, company: Company, exclude: int | None = None) -> list[int]:
    async with db.session() as s:
        users = (
            await s.scalars(select(AllowedUser).where(AllowedUser.role == UserRole.APPROVER))
        ).all()
    return [u.telegram_id for u in users if u.telegram_id != exclude and can_act_for(u, company)]


async def ask_second_approver(
    bot: Bot,
    db: Database,
    company: Company,
    first_approver: int,
    text: str,
    kb: InlineKeyboardMarkup | None,
) -> bool:
    """Send the item with its approve buttons to every other approver; False if there is none
    (or none could be reached)."""
    sent = False
    for chat in await approvers_for(db, company, exclude=first_approver):
        try:
            await bot.send_message(chat, text, reply_markup=kb)
            sent = True
        except Exception:  # a user who never started the bot can't be messaged
            log.warning("could not ask approver %s for a second approval", chat, exc_info=True)
    return sent
