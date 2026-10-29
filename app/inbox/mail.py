"""Reading emails: a read-only IMAP fetch (the mailbox is opened with EXAMINE, so nothing is
marked as read or changed) and parsing of the parts we need. Emails are untrusted input."""

import email
import imaplib
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import parseaddr, parsedate_to_datetime

from app.config import ImapMailbox

MAX_MESSAGE_BYTES = 25 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_PER_RUN = 50
MIN_IMAGE_BYTES = 20 * 1024
TYPES = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_EXT_TYPES = {ext: mt for mt, ext in TYPES.items()} | {".jpeg": "image/jpeg"}


class MailError(Exception):
    """A user-facing mailbox problem (login failed, folder missing, network)."""


@dataclass
class RawMail:
    uid: int
    data: bytes


@dataclass
class FileAttachment:
    filename: str
    media_type: str
    data: bytes = field(repr=False)


@dataclass
class ParsedMail:
    message_id: str | None
    sender: str  # address only, lower-case
    sender_name: str
    subject: str
    received_at: datetime | None
    auth: str  # pass | fail | unverified
    attachments: list[FileAttachment]


def _ok(typ: str, data, what: str) -> None:  # noqa: ANN001
    if typ != "OK":
        detail = data[0].decode(errors="replace") if data and data[0] else ""
        raise MailError(f"IMAP {what} failed: {detail}".strip())


def fetch_new(
    box: ImapMailbox, last_uid: int | None, uid_validity: int | None, since: date
) -> tuple[int, list[RawMail]]:
    """New messages after last_uid (same UIDVALIDITY) or, on a first run / reset mailbox, since
    the given date. Blocking: run it in a thread."""
    try:
        conn = imaplib.IMAP4_SSL(box.host, box.port, timeout=30)
    except OSError as e:
        raise MailError(f"Cannot connect to {box.host}:{box.port} ({e})") from e
    try:
        try:
            conn.login(box.user, box.password)
        except imaplib.IMAP4.error as e:
            raise MailError(
                f"Login to {box.host} as {box.user} was refused. For Gmail use an app "
                "password (Google account → Security → App passwords) and enable IMAP."
            ) from e
        typ, data = conn.select(_quote(box.folder), readonly=True)
        _ok(typ, data, f"opening folder {box.folder}")
        validity = int((conn.untagged_responses.get("UIDVALIDITY") or [b"0"])[0])
        if last_uid and uid_validity == validity:
            typ, data = conn.uid("SEARCH", None, f"UID {last_uid + 1}:*")
        else:
            typ, data = conn.uid("SEARCH", None, f"SINCE {since.strftime('%d-%b-%Y')}")
        _ok(typ, data, "search")
        uids = sorted(int(u) for u in (data[0] or b"").split())
        if last_uid and uid_validity == validity:
            uids = [u for u in uids if u > last_uid]  # "N:*" always returns the last message
        out: list[RawMail] = []
        for uid in uids[:MAX_PER_RUN]:
            typ, data = conn.uid("FETCH", str(uid), "(RFC822.SIZE)")
            _ok(typ, data, "size")
            m = re.search(rb"RFC822\.SIZE (\d+)", data[0] if isinstance(data[0], bytes) else b"")
            if m and int(m.group(1)) > MAX_MESSAGE_BYTES:
                out.append(RawMail(uid, b""))  # too big: remembered, not read
                continue
            typ, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
            _ok(typ, data, "fetch")
            body = next((p[1] for p in data if isinstance(p, tuple)), b"")
            out.append(RawMail(uid, body))
        return validity, out
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: S110 - closing a dead connection
            pass


def check_login(box: ImapMailbox) -> int:
    """Connect, log in and open the folder; returns the number of messages in it."""
    conn = None
    try:
        conn = imaplib.IMAP4_SSL(box.host, box.port, timeout=20)
        conn.login(box.user, box.password)
        typ, data = conn.select(_quote(box.folder), readonly=True)
        _ok(typ, data, f"opening folder {box.folder}")
        return int(data[0] or 0)
    except imaplib.IMAP4.error as e:
        raise MailError(f"Login or folder failed: {e}") from e
    except OSError as e:
        raise MailError(f"Cannot connect to {box.host}:{box.port} ({e})") from e
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:  # noqa: S110
                pass


def _quote(folder: str) -> str:
    return f'"{folder}"' if " " in folder and not folder.startswith('"') else folder


def auth_result(msg: EmailMessage) -> str:
    """SPF/DKIM/DMARC verdict from the Authentication-Results header added by OUR mail
    provider. Only the topmost header counts: lower ones could have been written by the
    sender. pass = DMARC pass, or DKIM and SPF both pass; fail = DMARC fail or both fail."""
    headers = msg.get_all("Authentication-Results") or []
    if not headers:
        return "unverified"
    top = str(headers[0]).lower()

    def verdict(kind: str) -> str | None:
        m = re.search(rf"\b{kind}=(\w+)", top)
        return m.group(1) if m else None

    dmarc, dkim, spf = verdict("dmarc"), verdict("dkim"), verdict("spf")
    if dmarc == "pass" or (dkim == "pass" and spf == "pass"):
        return "pass"
    if dmarc == "fail" or (dkim in ("fail", "none") and spf in ("fail", "softfail")):
        return "fail"
    return "unverified"


def parse(raw: bytes) -> ParsedMail:
    msg: EmailMessage = email.message_from_bytes(raw, policy=default_policy)  # type: ignore[assignment]
    name, addr = parseaddr(str(msg.get("From", "")))
    try:
        received = parsedate_to_datetime(str(msg["Date"])) if msg["Date"] else None
        if received and received.tzinfo is None:
            received = received.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        received = None
    files: list[FileAttachment] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename() or ""
        media = part.get_content_type()
        if media not in TYPES:  # e.g. application/octet-stream "invoice.pdf"
            ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
            media = _EXT_TYPES.get(ext, "")
        if not media:
            continue
        inline = part.get_content_disposition() != "attachment" or bool(part.get("Content-ID"))
        if media != "application/pdf" and inline:
            continue  # logos and pictures in the email body / signature
        try:
            data = part.get_payload(decode=True) or b""
        except Exception:  # noqa: S112 - broken part
            continue
        if not data or len(data) > MAX_ATTACHMENT_BYTES:
            continue
        if media == "application/pdf" and not data.startswith(b"%PDF"):
            continue
        if media != "application/pdf" and len(data) < MIN_IMAGE_BYTES:
            continue  # icons and logos; a photographed invoice is much larger
        files.append(FileAttachment(filename or f"attachment{TYPES[media]}", media, data))
    return ParsedMail(
        message_id=(str(msg.get("Message-ID") or "").strip() or None),
        sender=addr.strip().lower(),
        sender_name=name.strip(),
        subject=str(msg.get("Subject") or "").strip(),
        received_at=received,
        auth=auth_result(msg),
        attachments=files[:5],
    )
