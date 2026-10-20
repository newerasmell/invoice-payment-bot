from datetime import UTC, datetime, timedelta
from decimal import Decimal

from aiogram.methods import AnswerCallbackQuery, SendMessage
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.bot.payments_flow import Pay
from app.db.models import (
    AllowedUser,
    BankTransfer,
    Company,
    Counterparty,
    Invoice,
    InvoiceStatus,
    MatchConfidence,
    PaymentMatch,
    TransferDirection,
    UserRole,
)
from app.invoices import service as svc
from app.payments import sync as psync
from tests.conftest import BotHarness


async def _sent_invoice(db, settings, amount: str = "1200", client: str = "Client Ltd") -> Invoice:  # noqa: ANN001
    async with db.session() as s:
        x = await s.scalar(
            select(Company).options(selectinload(Company.bank_accounts)).where(Company.code == "X")
        )
        c = await s.scalar(select(Counterparty).where(Counterparty.name == client))
        if c is None:
            c = Counterparty(name=client)
            s.add(c)
            await s.flush()
        inv = await svc.create_draft(
            s,
            settings,
            issuer=x,
            client=c,
            vat_rate=Decimal(0),
            lines=[svc.LineInput("Work", Decimal(1), Decimal(amount))],
        )
        await s.commit()
        await svc.approve(s, settings, await svc.load_bundle(s, inv.id), 1001)
        return inv


def texts_of(calls: list) -> list[str]:
    return [c.text for c in calls if isinstance(c, SendMessage)]


async def test_reference_payment_marks_invoice_paid(harness: BotHarness, db, settings) -> None:
    inv = await _sent_invoice(db, settings)
    calls = await harness.send_raw(
        f'/dev_simulate_incoming X 1200 "Client Ltd" Payment for {inv.number}'
    )
    sim, paid = texts_of(calls)
    assert "Received 1,200.00 EUR" in paid and inv.number in paid and "PAID" in paid
    async with db.session() as s:
        got = await s.get(Invoice, inv.id)
        bt = await s.scalar(select(BankTransfer))
        m = await s.scalar(select(PaymentMatch))
        assert got.status == InvoiceStatus.PAID and got.related_transfer_id == bt.id
        assert (m.confidence, m.method, m.invoice_id) == (
            MatchConfidence.HIGH,
            "reference+amount",
            inv.id,
        )
        assert bt.company_id is not None and bt.account == "41411111111"

    # Re-syncing the same bank data never processes it twice.
    events = await psync.sync(db, harness.dp["bank"], datetime.now(UTC) - timedelta(hours=1))
    assert events == []
    async with db.session() as s:
        assert await s.scalar(select(func.count(PaymentMatch.id))) == 1


async def test_unclear_payment_confirmed_by_button(harness: BotHarness, db, settings) -> None:
    a = await _sent_invoice(db, settings, "500", "Alpha Ltd")
    b = await _sent_invoice(db, settings, "500", "Beta Ltd")
    calls = await harness.send_raw('/dev_simulate_incoming X 500 "Somebody Else" thanks')
    [review] = [c for c in calls if isinstance(c, SendMessage) and "Which invoice" in c.text]
    data = [btn.callback_data for row in review.reply_markup.inline_keyboard for btn in row]
    async with db.session() as s:
        bt_id = await s.scalar(select(BankTransfer.id))
    assert data == [
        Pay(action="ok", t=bt_id, i=a.id).pack(),
        Pay(action="ok", t=bt_id, i=b.id).pack(),
        Pay(action="no", t=bt_id).pack(),
    ]

    # Operators can't confirm; approvers can, once.
    async with db.session() as s:
        s.add(AllowedUser(telegram_id=2002, role=UserRole.OPERATOR))
        await s.commit()
    calls = await harness.click(data[1], user_id=2002)
    assert any(
        isinstance(c, AnswerCallbackQuery) and "Only approvers" in (c.text or "") for c in calls
    )
    calls = await harness.click(data[1])
    assert any(f"invoice <b>{b.number}</b>" in t for t in texts_of(calls))
    calls = await harness.click(data[0])
    assert any(
        isinstance(c, AnswerCallbackQuery) and "Already resolved" in (c.text or "") for c in calls
    )
    async with db.session() as s:
        assert (await s.get(Invoice, b.id)).status == InvoiceStatus.PAID
        assert (await s.get(Invoice, a.id)).status == InvoiceStatus.APPROVED
        m = await s.scalar(select(PaymentMatch))
        assert m.invoice_id == b.id and m.confirmed_by_user == 1001


async def test_dismiss_and_payments_command(harness: BotHarness, db, settings) -> None:
    await harness.send_raw('/dev_simulate_incoming X 99 "Random Payer" refund')
    async with db.session() as s:
        bt_id = await s.scalar(select(BankTransfer.id))
    await harness.click(Pay(action="no", t=bt_id).pack())
    async with db.session() as s:
        assert (await s.scalar(select(PaymentMatch))).method == "dismissed"
    inv = await _sent_invoice(db, settings)
    await harness.send_raw(f'/dev_simulate_incoming X 1200 "Client Ltd" {inv.number}')
    await harness.send_raw('/dev_simulate_outgoing X 50 "Supplier Ltd" BG80BNBG96611020345678 fee')
    [listing] = [t for t in texts_of(await harness.send_raw("/payments")) if "Recent bank" in t]
    assert f"✅ {inv.number}" in listing and "99.00 EUR" in listing and "➡️" in listing


async def test_pending_then_processed(db, bank, settings) -> None:
    await _sent_invoice(db, settings)
    t = await bank.simulate(
        direction=TransferDirection.INCOMING,
        account="41411111111",
        account_holder="Company X Ltd",
        amount=Decimal("1200"),
        currency="EUR",
        counterparty_name="Client Ltd",
        reference="0000000001",
        status="pending",
    )
    assert await psync.sync_one(db, bank, t.external_id) == []
    async with db.session() as s:
        from app.db.models import MockBankTransfer

        row = await s.scalar(select(MockBankTransfer))
        row.status = "processed"
        await s.commit()
    [ev] = await psync.sync_one(db, bank, t.external_id)
    assert ev.kind == "paid"


def test_webhook_refetches_and_processes(admin_settings_factory) -> None:  # noqa: ANN001
    import asyncio
    import time

    settings, db_url = admin_settings_factory
    from app.main import create_app

    with TestClient(create_app(settings)) as c:
        app = c.app
        bank = app.state.bank

        async def make() -> str:
            t = await bank.simulate(
                direction=TransferDirection.INCOMING,
                account="41411111111",
                account_holder="Company X Ltd",
                amount=Decimal("10"),
                currency="EUR",
                counterparty_name="Someone",
            )
            return t.external_id

        tid = c.portal.call(make) if hasattr(c, "portal") else asyncio.run(make())
        r = c.post(
            "/webhooks/paynetics",
            json={"transferId": tid, "transferStatus": "processed", "amount": "999999.00"},
        )
        assert r.json() == {"ok": True, "received": 1}
        assert c.post("/webhooks/paynetics", content=b"not json").status_code == 200
        for _ in range(50):

            async def count() -> int:
                async with app.state.db.session() as s:
                    return await s.scalar(select(func.count(BankTransfer.id)))

            if c.portal.call(count):
                break
            time.sleep(0.05)

        async def stored() -> BankTransfer:
            async with app.state.db.session() as s:
                return await s.scalar(select(BankTransfer))

        bt = c.portal.call(stored)
        # The pushed amount was ignored; the re-fetched transfer is what got stored.
        assert bt.amount == Decimal("10.00") and bt.processed_at is not None
