import imaplib
from collections.abc import Iterator
from datetime import date, timedelta
from email.message import EmailMessage
from pathlib import Path

import pytest
from aiogram.methods import SendDocument, SendMessage
from sqlalchemy import select

from app.bot.documents_flow import Ins
from app.bot.inbox_flow import Mail, notify_mail
from app.config import ImapMailbox
from app.db.models import (
    AuditLog,
    Counterparty,
    InboxMessage,
    InboxStatus,
    InstructionStatus,
    PaymentInstruction,
)
from app.inbox import mail
from app.inbox import service as inbox
from tests.conftest import BotHarness
from tests.fake_imap import FakeImap
from tests.test_documents import doc

PDF = b"%PDF-1.4 fake invoice"


def email_bytes(
    *,
    sender: str = "billing@supplier.test",
    subject: str = "Invoice INV-2026-005",
    auth: list[str] | None = None,
    pdf: bytes | None = PDF,
    octet_name: str | None = None,
    inline_png: bool = False,
    photo: bool = False,
) -> bytes:
    m = EmailMessage()
    m["From"] = f"Supplier Ltd <{sender}>"
    m["To"] = "inv@x.test"
    m["Subject"] = subject
    m["Date"] = "Thu, 22 Oct 2026 10:00:00 +0300"
    m["Message-ID"] = "<abc@supplier.test>"
    for h in auth or []:
        m["Authentication-Results"] = h
    m.set_content("Please find the invoice attached.")
    if pdf is not None:
        m.add_attachment(pdf, maintype="application", subtype="pdf", filename="INV-2026-005.pdf")
    if octet_name:
        m.add_attachment(PDF, maintype="application", subtype="octet-stream", filename=octet_name)
    if inline_png:  # a logo in the signature
        m.add_attachment(
            b"\x89PNG" + b"0" * 30000,
            maintype="image",
            subtype="png",
            filename="logo.png",
            disposition="inline",
            cid="<logo@x>",
        )
    if photo:  # a photographed invoice attached as a file
        m.add_attachment(
            b"\xff\xd8" + b"1" * 40000, maintype="image", subtype="jpeg", filename="IMG_2041.jpg"
        )
    return m.as_bytes()


def test_auth_results_top_header_only() -> None:
    def verdict(*headers: str) -> str:
        return mail.parse(email_bytes(auth=list(headers))).auth

    assert verdict("mx.google.com; dkim=pass header.i=@s.test; spf=pass; dmarc=pass") == "pass"
    assert verdict("mx; dkim=pass; spf=pass") == "pass"
    assert verdict("mx; dkim=fail; spf=softfail") == "fail"
    assert verdict("mx; dmarc=fail") == "fail"
    assert verdict("mx; spf=neutral") == "unverified"
    assert verdict() == "unverified"
    # A sender can add its own header below ours: only the topmost one counts.
    assert verdict("mx; spf=fail; dkim=none", "forged; dkim=pass; spf=pass; dmarc=pass") == "fail"


def test_attachments() -> None:
    p = mail.parse(email_bytes(octet_name="scan.PDF", inline_png=True, photo=True))
    assert [(a.filename, a.media_type) for a in p.attachments] == [
        ("INV-2026-005.pdf", "application/pdf"),
        ("scan.PDF", "application/pdf"),
        ("IMG_2041.jpg", "image/jpeg"),
    ]
    assert mail.parse(email_bytes(pdf=None, inline_png=True)).attachments == []
    assert (p.sender, p.subject, p.message_id) == (
        "billing@supplier.test",
        "Invoice INV-2026-005",
        "<abc@supplier.test>",
    )
    assert mail.parse(email_bytes(pdf=b"not really a pdf")).attachments == []


@pytest.fixture
def imap(monkeypatch) -> Iterator[FakeImap]:  # noqa: ANN001
    server = FakeImap()
    monkeypatch.setattr(
        mail.imaplib, "IMAP4_SSL", lambda host, port, timeout=None: imaplib.IMAP4(host, port)
    )
    yield server
    server.close()


def _box(imap: FakeImap, password: str = "app-pass") -> ImapMailbox:
    return ImapMailbox(host="127.0.0.1", port=imap.port, user="inv@x.test", password=password)


def test_login_and_read_only(imap: FakeImap) -> None:
    imap.messages[1] = (date.today(), email_bytes())
    assert mail.check_login(_box(imap)) == 1
    with pytest.raises(mail.MailError, match="app password"):
        mail.fetch_new(_box(imap, "wrong"), None, None, date.today())
    validity, raws = mail.fetch_new(_box(imap), None, None, date.today())
    assert validity == 1000 and [r.uid for r in raws] == [1]
    assert imap.flag_changes == 0  # EXAMINE + BODY.PEEK: nothing marked as read


async def test_poll_stores_once_and_follows_uids(imap: FakeImap, db, settings) -> None:
    old = date.today() - timedelta(days=30)
    imap.messages[1] = (old, email_bytes())  # older than the first-run lookback
    imap.messages[2] = (date.today(), email_bytes())
    imap.messages[3] = (date.today(), email_bytes(pdf=None))  # no attachment
    box = _box(imap)
    ids = await inbox.poll_mailbox(db, settings, "X", box)
    assert len(ids) == 1
    async with db.session() as s:
        rows = (await s.scalars(select(InboxMessage).order_by(InboxMessage.uid))).all()
        assert [(r.uid, r.status) for r in rows] == [(2, InboxStatus.NEW), (3, InboxStatus.SKIPPED)]
        f = rows[0].attachments[0]
        assert f["name"] == "INV-2026-005.pdf" and Path(f["path"]).read_bytes() == PDF  # noqa: ASYNC240
    assert await inbox.poll_mailbox(db, settings, "X", box) == []  # nothing new
    imap.messages[4] = (date.today(), email_bytes(subject="Second"))
    assert len(await inbox.poll_mailbox(db, settings, "X", box)) == 1
    imap.uidvalidity = 2000  # mailbox rebuilt by the provider: start again from the date
    imap.messages = {7: (date.today(), email_bytes(subject="After reset"))}
    assert len(await inbox.poll_mailbox(db, settings, "X", box)) == 1


async def test_poll_all_records_errors(imap: FakeImap, db, settings, monkeypatch) -> None:
    monkeypatch.setenv("IMAP_X_HOST", "127.0.0.1")
    monkeypatch.setenv("IMAP_X_PORT", str(imap.port))
    monkeypatch.setenv("IMAP_X_USER", "inv@x.test")
    monkeypatch.setenv("IMAP_X_PASSWORD", "wrong")
    assert await inbox.poll_all(db, settings) == []
    assert "refused" in inbox.STATUS["X"].error
    monkeypatch.setenv("IMAP_X_PASSWORD", "app-pass")
    imap.messages[1] = (date.today(), email_bytes())
    assert len(await inbox.poll_all(db, settings)) == 1 and inbox.STATUS["X"].error is None


async def test_telegram_read_and_prepare(harness: BotHarness, imap: FakeImap, db, settings) -> None:
    imap.messages[1] = (date.today(), email_bytes(auth=["mx; spf=neutral"]))
    imap.messages[2] = (date.today(), email_bytes(subject="Spam"))
    first, second = await inbox.poll_mailbox(db, settings, "X", _box(imap))
    async with db.session() as s:
        s.add(Counterparty(name="Supplier Ltd", email="billing@supplier.test"))
        await s.commit()
    before = len(harness.session.requests)
    await notify_mail(harness.bot, db, first)
    [sent] = [c for c in harness.calls_since(before) if isinstance(c, SendDocument)]
    assert "Invoice by email</b> to <b>X</b>" in sent.caption
    assert "sender not verified" in sent.caption and "Known as: <b>Supplier Ltd</b>" in sent.caption

    harness.parser.doc_queue.append(doc())
    calls = await harness.click(Mail(action="rd", id=first).pack())
    [card] = [c for c in calls if isinstance(c, SendMessage) and "Payment instruction" in c.text]
    assert "confirm the IBAN with the supplier by phone" in card.text
    _, note, attachments = harness.parser.calls[-1]
    assert "mailbox of X" in note and attachments[0].data == PDF
    async with db.session() as s:
        m = await s.get(InboxMessage, first)
        instr = await s.get(PaymentInstruction, m.instruction_id)
        assert m.status == InboxStatus.PREPARED and instr.status == InstructionStatus.PROPOSED
        assert instr.document_path == m.attachments[0]["path"]
    assert Ins(action="ap", id=instr.id).pack() in str(card.reply_markup)
    calls = await harness.click(Mail(action="rd", id=first).pack())
    assert any("Already prepared" in (getattr(c, "text", "") or "") for c in calls)

    await harness.click(Mail(action="ig", id=second).pack())
    async with db.session() as s:
        assert (await s.get(InboxMessage, second)).status == InboxStatus.IGNORED
        actions = (
            await s.scalars(select(AuditLog.action).where(AuditLog.action.like("inbox.%")))
        ).all()
        assert {"inbox.received", "inbox.prepared", "inbox.ignored"} <= set(actions)
    assert "No supplier invoices waiting" in (await harness.send("/inbox"))[-1]
