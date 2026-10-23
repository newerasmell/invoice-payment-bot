"""Load older bank transfers for reporting (Admin → Finance → Load bank history).

Old transfers are stored for the record only: they are marked processed without matching,
rules or notifications, so loading a year of history never drafts invoices or pings anyone.
Transfers inside the normal polling window are left to the poller, which processes them as
usual.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from app.audit import audit
from app.bank.base import BankProvider
from app.bank.paynetics import PayneticsProvider
from app.config import Settings
from app.db.session import Database
from app.payments import sync as psync

log = logging.getLogger(__name__)
MAX_DAYS = 366


@dataclass
class HistoryState:
    running: bool = False
    since: date | None = None
    started: datetime | None = None
    finished: datetime | None = None
    seen: int = 0
    added: int = 0
    error: str | None = None
    log: list[str] = field(default_factory=list)


async def load_history(
    db: Database, bank: BankProvider, settings: Settings, since: date, state: HistoryState
) -> None:
    state.running, state.since, state.error = True, since, None
    state.started, state.finished, state.seen, state.added = datetime.now(UTC), None, 0, 0
    cutoff = datetime.now(UTC) - timedelta(hours=settings.poll_lookback_hours)
    try:
        async with psync._sync_lock:
            if isinstance(bank, PayneticsProvider):
                async with db.session() as s:
                    bank.accounts_by_code = await psync._accounts_by_code(s)
            transfers = await bank.list_transfers(datetime.combine(since, time.min, UTC))
        for t in transfers:
            state.seen += 1
            async with db.session() as s:
                existed = await psync.find_transfer(s, bank.name, t.external_id)
                bt = await psync.upsert_transfer(s, bank.name, t)
                booked = bt.booked_at
                if booked is not None and booked.tzinfo is None:
                    booked = booked.replace(tzinfo=UTC)
                if bt.processed_at is None and booked is not None and booked < cutoff:
                    bt.processed_at = datetime.now(UTC)  # history: recorded, not acted on
                if not existed:
                    state.added += 1
                await s.commit()
        async with db.session() as s:
            audit(
                s,
                "admin:web",
                "bank.history_loaded",
                None,
                {"since": since.isoformat(), "seen": state.seen, "added": state.added},
            )
            await s.commit()
    except Exception as e:  # shown on the Finance page
        log.exception("loading bank history failed")
        state.error = f"{type(e).__name__}: {e}"
    finally:
        state.running, state.finished = False, datetime.now(UTC)
