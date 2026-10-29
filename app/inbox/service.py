"""Flow D: invoices arriving by email in one of our companies' mailboxes.

Each new email is stored once (mailbox + UIDVALIDITY + UID). One with a PDF/image attachment
becomes a Telegram notice with Read & prepare payment / Ignore. Nothing is read by Claude and
no instruction is created until a human clicks. The email itself is never trusted.
"""

import asyncio
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit
from app.config import ImapMailbox, Settings
from app.db.models import Company, Counterparty, InboxMessage, InboxStatus
from app.db.session import Database
from app.inbox import mail

log = logging.getLogger(__name__)


@dataclass
class MailboxStatus:
    checked_at: datetime | None = None
    error: str | None = None
    new: int = 0


STATUS: dict[str, MailboxStatus] = {}


async def _company(session: AsyncSession, code: str) -> Company | None:
    for c in (await session.scalars(select(Company))).all():
        if c.code.upper().replace("-", "_") == code:
            return c
    return None


async def known_sender(session: AsyncSession, address: str) -> str | None:
    """The name of one of our companies or clients using this email address, if any."""
    if not address:
        return None
    ours = await session.scalar(select(Company).where(func.lower(Company.email_from) == address))
    if ours:
        return f"{ours.code} ({ours.legal_name or ours.name})"
    client = await session.scalar(
        select(Counterparty).where(func.lower(Counterparty.email) == address)
    )
    return client.name if client else None


async def _store_files(settings: Settings, files: list[mail.FileAttachment]) -> list[dict]:
    folder = settings.uploads_dir / "inbox"
    await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
    out = []
    for f in files:
        path = folder / f"{secrets.token_hex(8)}{mail.TYPES[f.media_type]}"
        await asyncio.to_thread(path.write_bytes, f.data)
        out.append(
            {
                "name": f.filename[:200],
                "media_type": f.media_type,
                "path": str(path),
                "size": len(f.data),
            }
        )
    return out


async def poll_mailbox(db: Database, settings: Settings, code: str, box: ImapMailbox) -> list[int]:
    """Fetch and store new emails of one mailbox; returns ids of the ones to show in Telegram."""
    async with db.session() as s:
        company = await _company(s, code)
        if company is None:
            raise mail.MailError(f"No company with code {code} for IMAP_{code}_*")
        company_id = company.id
        last = (
            await s.execute(
                select(InboxMessage.uid_validity, func.max(InboxMessage.uid))
                .where(InboxMessage.mailbox == box.key)
                .group_by(InboxMessage.uid_validity)
                .order_by(func.max(InboxMessage.id).desc())
                .limit(1)
            )
        ).first()
    validity, last_uid = (last[0], last[1]) if last else (None, None)
    since = date.today() - timedelta(days=settings.imap_lookback_days)
    validity_now, raws = await asyncio.to_thread(mail.fetch_new, box, last_uid, validity, since)
    new_ids: list[int] = []
    for raw in raws:
        parsed = mail.parse(raw.data) if raw.data else None
        files = await _store_files(settings, parsed.attachments) if parsed else []
        async with db.session() as s:
            row = InboxMessage(
                company_id=company_id,
                mailbox=box.key,
                uid_validity=validity_now,
                uid=raw.uid,
                message_id=parsed.message_id[:255] if parsed and parsed.message_id else None,
                sender=(parsed.sender[:255] if parsed else None),
                subject=(parsed.subject[:500] if parsed else None),
                received_at=parsed.received_at if parsed else None,
                auth=parsed.auth if parsed else "unverified",
                attachments=files,
                status=InboxStatus.NEW if files else InboxStatus.SKIPPED,
                detail=None if parsed else "message too big, not read",
            )
            # The same email can be copied to several of our mailboxes; each is a separate row.
            s.add(row)
            await s.flush()
            if files:
                audit(
                    s,
                    "system",
                    "inbox.received",
                    f"inbox:{row.id}",
                    {"company": code, "from": row.sender, "auth": row.auth, "files": len(files)},
                )
                new_ids.append(row.id)
            await s.commit()
    return new_ids


async def poll_all(db: Database, settings: Settings) -> list[int]:
    new: list[int] = []
    for code, box in settings.imap_mailboxes().items():
        st = STATUS.setdefault(code, MailboxStatus())
        try:
            ids = await poll_mailbox(db, settings, code, box)
            st.error, st.new = None, len(ids)
            new += ids
        except mail.MailError as e:
            st.error = str(e)
            log.warning("mailbox %s: %s", code, e)
        except Exception as e:  # one broken mailbox must not stop the others
            st.error = f"{type(e).__name__}: {e}"
            log.exception("mailbox %s failed", code)
        st.checked_at = datetime.now(UTC)
    return new


async def inbox_forever(settings: Settings, db: Database, bot) -> None:  # noqa: ANN001
    from app.bot.inbox_flow import notify_mail  # the bot is optional

    await asyncio.sleep(20)
    while True:
        try:
            for msg_id in await poll_all(db, settings):
                if bot is not None:
                    await notify_mail(bot, db, msg_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("inbox polling failed")
        await asyncio.sleep(settings.imap_poll_seconds)


async def pending(session: AsyncSession, company_ids: list[int] | None) -> list[InboxMessage]:
    q = select(InboxMessage).where(
        or_(InboxMessage.status == InboxStatus.NEW, InboxMessage.status == InboxStatus.FAILED)
    )
    if company_ids is not None:
        q = q.where(InboxMessage.company_id.in_(company_ids))
    return list((await session.scalars(q.order_by(InboxMessage.id.desc()).limit(20))).all())
