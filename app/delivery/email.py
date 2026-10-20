import asyncio
import smtplib
import ssl
from email.message import EmailMessage

from app.config import Settings


class EmailNotConfigured(RuntimeError):
    pass


def _send(settings: Settings, msg: EmailMessage) -> None:
    assert settings.smtp_host
    ctx = ssl.create_default_context()
    if settings.smtp_port == 465:
        server: smtplib.SMTP = smtplib.SMTP_SSL(settings.smtp_host, 465, context=ctx, timeout=30)
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
    with server:
        if settings.smtp_port != 465 and settings.smtp_starttls:
            server.starttls(context=ctx)
        if settings.smtp_username and settings.smtp_password:
            server.login(settings.smtp_username, settings.smtp_password.get_secret_value())
        server.send_message(msg)


async def send_email(
    settings: Settings,
    *,
    sender: str | None,
    to: str,
    subject: str,
    body: str,
    attachment: bytes,
    filename: str,
) -> None:
    if not settings.smtp_host:
        raise EmailNotConfigured("SMTP is not configured")
    from_addr = sender or settings.smtp_from
    if not from_addr:
        raise EmailNotConfigured("no sender address (company email or SMTP_FROM)")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = from_addr, to, subject
    msg.set_content(body)
    msg.add_attachment(attachment, maintype="application", subtype="pdf", filename=filename)
    await asyncio.to_thread(_send, settings, msg)
