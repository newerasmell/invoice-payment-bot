import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User
from sqlalchemy import select

from app.db.models import AllowedUser
from app.db.session import Database

log = logging.getLogger(__name__)


class WhitelistMiddleware(BaseMiddleware):
    """Drops every update from users not in AllowedUser, silently. Injects `allowed_user`."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None:
            return None
        async with self.db.session() as s:
            allowed = await s.scalar(select(AllowedUser).where(AllowedUser.telegram_id == user.id))
        if allowed is None:
            log.info("ignored update from non-whitelisted telegram user %s", user.id)
            return None
        data["allowed_user"] = allowed
        return await handler(event, data)
