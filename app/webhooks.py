"""POST /webhooks/paynetics — transfer status change hints.

The webhook is unsigned and retried only once, so its payload is never trusted: we take the
transferId, re-fetch the transfer from the API and process that. Always answer 200 quickly.
"""

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Request

from app.bot import payments_flow
from app.payments import sync as psync

log = logging.getLogger(__name__)
router = APIRouter()
_tasks: set[asyncio.Task[None]] = set()
MAX_IDS_PER_CALL = 50


def _transfer_ids(body: Any) -> list[str]:
    items = body if isinstance(body, list) else [body]
    ids = []
    for item in items[:MAX_IDS_PER_CALL]:
        if isinstance(item, dict):
            tid = item.get("transferId")
            if isinstance(tid, str | int) and 0 < len(str(tid)) <= 64:
                ids.append(str(tid))
    return list(dict.fromkeys(ids))


async def _handle(app: Any, ids: list[str]) -> None:
    for tid in ids:
        try:
            events = await psync.sync_one(app.state.db, app.state.bank, tid)
            await payments_flow.notify(getattr(app.state, "bot", None), app.state.db, events)
        except Exception:
            log.exception("webhook: could not process transfer %s", tid)


@router.post("/webhooks/paynetics", include_in_schema=False)
async def paynetics_webhook(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        form = await request.form()
        body = dict(form)
    ids = _transfer_ids(body)
    log.info("paynetics webhook: %d transfer id(s)", len(ids))
    if ids:
        task = asyncio.create_task(_handle(request.app, ids))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    return {"ok": True, "received": len(ids)}
