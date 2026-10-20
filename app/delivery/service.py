"""Deliver an approved invoice to the client over the channels on the client's profile.

DRY_RUN never reaches the client: email goes to DRY_RUN_EMAIL (or is only logged) and the
Telegram message goes to the operator who pressed Send, marked as a dry run.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from aiogram import Bot
from aiogram.types import BufferedInputFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit
from app.config import Settings
from app.db.models import DeliveryChannel, InvoiceDelivery, InvoiceStatus
from app.delivery import messages
from app.delivery.email import EmailNotConfigured, send_email
from app.invoices.service import (
    InvoiceBundle,
    InvoiceError,
    pdf_filename,
    public_url,
    stored_pdf,
)

log = logging.getLogger(__name__)


@dataclass
class DeliveryResult:
    channel: str
    recipient: str
    status: str  # sent | dry_run | failed | skipped
    detail: str = ""


def client_channels(b: InvoiceBundle) -> list[str]:
    c = b.client
    wanted = c.delivery_channels or [
        ch
        for ch, ok in (
            (DeliveryChannel.EMAIL.value, c.email),
            (DeliveryChannel.TELEGRAM.value, c.telegram_chat_id),
        )
        if ok
    ]
    return [ch for ch in wanted if ch in {d.value for d in DeliveryChannel}]


async def deliver(
    session: AsyncSession,
    settings: Settings,
    bot: Bot | None,
    b: InvoiceBundle,
    *,
    actor_tg: int,
    operator_chat_id: int | None,
    resend: bool = False,
) -> list[DeliveryResult]:
    inv = b.invoice
    if inv.status == InvoiceStatus.SENT and not resend:
        raise InvoiceError("This invoice was already sent")
    if inv.status not in (InvoiceStatus.APPROVED, InvoiceStatus.SENT, InvoiceStatus.PAID):
        raise InvoiceError("Only approved invoices can be sent")
    channels = client_channels(b)
    if not channels:
        raise InvoiceError(
            f"{b.client.name} has no delivery channel. Add an email or Telegram chat id to the "
            "client profile in the admin panel."
        )
    pdf = await stored_pdf(session, b)
    link = public_url(settings, inv)
    results: list[DeliveryResult] = []
    for ch in channels:
        if ch == DeliveryChannel.EMAIL.value:
            results.append(await _email(settings, b, pdf, link))
        elif ch == DeliveryChannel.TELEGRAM.value:
            results.append(await _telegram(settings, bot, b, pdf, link, operator_chat_id))
    actor = f"tg:{actor_tg}"
    for r in results:
        session.add(
            InvoiceDelivery(
                invoice_id=inv.id,
                channel=r.channel,
                recipient=r.recipient,
                status=r.status,
                detail=r.detail or None,
                actor=actor,
            )
        )
    if (
        any(r.status in ("sent", "dry_run") for r in results)
        and inv.status == InvoiceStatus.APPROVED
    ):
        inv.status = InvoiceStatus.SENT
        inv.sent_at = datetime.now(UTC)
    audit(
        session,
        actor,
        "invoice.resent" if resend else "invoice.sent",
        f"invoice:{inv.id}",
        {"dry_run": settings.dry_run, "results": [f"{r.channel}:{r.status}" for r in results]},
    )
    return results


async def _email(
    settings: Settings, b: InvoiceBundle, pdf: bytes, link: str | None
) -> DeliveryResult:
    to = b.client.email
    if not to:
        return DeliveryResult("email", "-", "skipped", "client has no email")
    if settings.dry_run:
        if not settings.dry_run_email:
            log.info("[DRY RUN] email for invoice %s to %s logged only", b.invoice.number, to)
            return DeliveryResult("email", to, "dry_run", "logged only (DRY_RUN_EMAIL not set)")
        target, detail = settings.dry_run_email, f"redirected from {to}"
    else:
        target, detail = to, ""
    try:
        await send_email(
            settings,
            sender=b.issuer.email_from,
            to=target,
            subject=("[DRY RUN] " if settings.dry_run else "")
            + messages.subject(b.invoice, b.issuer),
            body=messages.body(b.invoice, b.issuer, b.client, link),
            attachment=pdf,
            filename=pdf_filename(b.invoice),
        )
    except EmailNotConfigured as e:
        return DeliveryResult("email", target, "skipped", str(e))
    except Exception as e:  # SMTP errors are reported, not raised
        log.exception("email delivery failed")
        return DeliveryResult("email", target, "failed", str(e)[:300])
    return DeliveryResult("email", target, "dry_run" if settings.dry_run else "sent", detail)


async def _telegram(
    settings: Settings,
    bot: Bot | None,
    b: InvoiceBundle,
    pdf: bytes,
    link: str | None,
    operator_chat_id: int | None,
) -> DeliveryResult:
    chat = b.client.telegram_chat_id
    if not chat:
        return DeliveryResult("telegram", "-", "skipped", "client has no Telegram chat id")
    if bot is None:
        return DeliveryResult("telegram", str(chat), "skipped", "bot is not running")
    caption = messages.telegram_caption(b.invoice, b.issuer, link)
    if settings.dry_run:
        if operator_chat_id is None:
            return DeliveryResult("telegram", str(chat), "dry_run", "logged only")
        target, caption = operator_chat_id, f"[DRY RUN] would go to chat {chat}\n\n{caption}"
    else:
        target = chat
    try:
        await bot.send_document(
            target,
            BufferedInputFile(pdf, filename=pdf_filename(b.invoice)),
            caption=caption,
            parse_mode=None,
        )
    except Exception as e:  # e.g. the client never started the bot
        log.warning("telegram delivery failed: %s", e)
        return DeliveryResult("telegram", str(chat), "failed", str(e)[:300])
    return DeliveryResult("telegram", str(chat), "dry_run" if settings.dry_run else "sent")
