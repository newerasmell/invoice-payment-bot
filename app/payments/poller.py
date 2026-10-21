import asyncio
import logging
from datetime import UTC, datetime, timedelta

from aiogram import Bot

from app.bank.base import BankAuthError, BankProvider
from app.config import Settings
from app.db.session import Database
from app.payments import sync as psync

log = logging.getLogger(__name__)


async def poll_forever(
    settings: Settings, db: Database, bank: BankProvider, bot: Bot | None
) -> None:
    from app.bot.payments_flow import notify  # avoid an import cycle at module load

    await asyncio.sleep(10)  # let the app finish starting
    while True:
        try:
            since = datetime.now(UTC) - timedelta(hours=settings.poll_lookback_hours)
            events = await psync.sync(db, bank, since, settings)
            if events:
                log.info("poll: %d payment event(s)", len(events))
            await notify(bot, db, events)
        except asyncio.CancelledError:
            raise
        except BankAuthError as e:
            log.error("poll: %s", e)
        except Exception:
            log.exception("poll failed")
        await asyncio.sleep(settings.poll_interval_seconds)
