from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.models import Company, Counterparty, InvoiceDelivery, InvoiceStatus
from app.delivery import service as dsvc
from app.invoices import service as svc
from tests.conftest import CLIENT_DETAILS, FakeTelegramSession


async def _approved(db, settings, **client_kw):  # noqa: ANN001, ANN003, ANN202
    async with db.session() as s:
        issuer = await s.scalar(
            select(Company).options(selectinload(Company.bank_accounts)).where(Company.code == "X")
        )
        client = Counterparty(name="Client Ltd", contact_name="Ana", **CLIENT_DETAILS, **client_kw)
        s.add(client)
        await s.flush()
        inv = await svc.create_draft(
            s,
            settings,
            issuer=issuer,
            client=client,
            lines=[svc.LineInput("Consulting", Decimal(1), Decimal(100))],
        )
        await s.commit()
        b = await svc.load_bundle(s, inv.id)
        await svc.approve(s, settings, b, 1001)
        return inv.id


async def _deliver(db, settings, inv_id, bot=None, **kw):  # noqa: ANN001, ANN003, ANN202
    async with db.session() as s:
        b = await svc.load_bundle(s, inv_id)
        res = await dsvc.deliver(s, settings, bot, b, actor_tg=1001, operator_chat_id=1001, **kw)
        await s.commit()
        return res


async def test_dry_run_email_logged_only(db, settings) -> None:
    inv_id = await _approved(db, settings, email="ap@client.example")
    [r] = await _deliver(db, settings, inv_id)
    assert (r.channel, r.recipient, r.status) == ("email", "ap@client.example", "dry_run")
    async with db.session() as s:
        b = await svc.load_bundle(s, inv_id)
        assert b.invoice.status == InvoiceStatus.SENT and b.invoice.sent_at
        assert (await s.scalar(select(InvoiceDelivery))).status == "dry_run"
    with pytest.raises(svc.InvoiceError, match="already sent"):
        await _deliver(db, settings, inv_id)


async def test_dry_run_email_redirected(db, settings, monkeypatch) -> None:
    sent = []

    async def fake_send(_settings, **kw):  # noqa: ANN001, ANN003, ANN202
        sent.append(kw)

    monkeypatch.setattr(dsvc, "send_email", fake_send)
    s2 = settings.model_copy(update={"dry_run_email": "me@ours.example"})
    inv_id = await _approved(db, s2, email="ap@client.example")
    [r] = await _deliver(db, s2, inv_id)
    assert r.status == "dry_run" and r.recipient == "me@ours.example"
    assert sent[0]["to"] == "me@ours.example" and sent[0]["subject"].startswith("[DRY RUN]")
    assert sent[0]["attachment"].startswith(b"%PDF")
    assert "Dear Ana," in sent[0]["body"] and "https://bot.example.com/i/" in sent[0]["body"]
    assert "IBAN: BG86PAYN96611000000001" in sent[0]["body"]


async def test_live_email_without_smtp_is_skipped(db, settings) -> None:
    live = settings.model_copy(update={"dry_run": False})
    inv_id = await _approved(db, live, email="ap@client.example")
    [r] = await _deliver(db, live, inv_id)
    assert r.status == "skipped" and "SMTP" in r.detail
    async with db.session() as s:
        assert (await svc.load_bundle(s, inv_id)).invoice.status == InvoiceStatus.APPROVED


async def test_telegram_dry_run_goes_to_operator(db, settings) -> None:
    from app.bot.setup import make_bot

    session = FakeTelegramSession()
    bot = make_bot("42:TEST", session=session)
    inv_id = await _approved(db, settings, telegram_chat_id=555, delivery_channels=["telegram"])
    [r] = await _deliver(db, settings, inv_id, bot=bot)
    assert r.status == "dry_run"
    [call] = session.requests
    assert call.chat_id == 1001 and "[DRY RUN] would go to chat 555" in call.caption


async def test_live_telegram_goes_to_client(db, settings) -> None:
    from app.bot.setup import make_bot

    session = FakeTelegramSession()
    bot = make_bot("42:TEST", session=session)
    live = settings.model_copy(update={"dry_run": False})
    inv_id = await _approved(db, live, telegram_chat_id=555, delivery_channels=["telegram"])
    [r] = await _deliver(db, live, inv_id, bot=bot)
    assert r.status == "sent" and session.requests[0].chat_id == 555
    assert session.requests[0].document.filename == "invoice-0000000001.pdf"


async def test_no_channel(db, settings) -> None:
    inv_id = await _approved(db, settings)
    with pytest.raises(svc.InvoiceError, match="no delivery channel"):
        await _deliver(db, settings, inv_id)
