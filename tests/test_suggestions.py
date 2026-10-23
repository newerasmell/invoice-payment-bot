from datetime import UTC, date, datetime
from decimal import Decimal

from aiogram.methods import SendMessage
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app import suggestions
from app.bot.report_flow import period_from_arg
from app.db.models import AuditLog, Company, Counterparty, Invoice, InvoiceStatus, TransferDirection
from app.invoices import service as svc
from app.payments import sync as psync
from app.reminders import maybe_weekly_digest
from tests.conftest import CLIENT_DETAILS, BotHarness


async def _invoice(db, settings, amount: str, client: str) -> Invoice:  # noqa: ANN001
    async with db.session() as s:
        x = await s.scalar(
            select(Company).options(selectinload(Company.bank_accounts)).where(Company.code == "X")
        )
        c = Counterparty(name=client, **CLIENT_DETAILS)
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


async def _pay(db, bank, settings, amount: str, who: str, ref: str = "thanks") -> int:  # noqa: ANN001
    t = await bank.simulate(
        direction=TransferDirection.INCOMING,
        account="41411111111",
        account_holder="Company X Ltd",
        amount=Decimal(amount),
        currency="EUR",
        counterparty_name=who,
        reference=ref,
    )
    await psync.sync_one(db, bank, t.external_id, settings)
    async with db.session() as s:
        from app.db.models import BankTransfer

        return await s.scalar(select(func.max(BankTransfer.id)))


async def test_suggestions_and_actions(db, bank, settings) -> None:
    a = await _invoice(db, settings, "2300", "Northwind Trading Ltd")
    b = await _invoice(db, settings, "750", "Blue Harbor OOD")
    c = await _invoice(db, settings, "9900", "Acme Logistics EAD")
    fees = await _pay(db, bank, settings, "2295.50", "Northwind Trading Ltd")
    s1 = await _pay(db, bank, settings, "400", "Blue Harbor OOD")
    s2 = await _pay(db, bank, settings, "350", "Blue Harbor OOD")
    await _pay(db, bank, settings, "9895", "Acme Logistics EAD", ref=f"{c.number}")
    await _pay(db, bank, settings, "77", "Nobody Ltd")
    async with db.session() as s:
        tips = await suggestions.build(s, date.today())
        [link] = tips.links
        assert (link.transfer.id, link.invoice.id) == (fees, a.id)
        assert "payer is the client" in link.reasons and "bank charges" in link.reasons[0]
        [split] = tips.splits
        assert split.invoice.id == b.id and {t.id for t in split.transfers} == {s1, s2}
        [(small, _)] = tips.small_remainders
        assert small.id == c.id and small.remaining == Decimal("5.00")
        assert tips.unmatched_without_idea == 1  # the 77.00 from nobody

        from app.db.models import BankTransfer

        inv_b = await s.get(Invoice, b.id)
        for tid in (s1, s2):
            await psync.link_transfer(s, await s.get(BankTransfer, tid), inv_b, "admin:web")
        await psync.close_remainder(s, await s.get(Invoice, c.id), "admin:web")
        await s.commit()
    async with db.session() as s:
        assert (await s.get(Invoice, b.id)).status == InvoiceStatus.PAID
        assert (await s.get(Invoice, c.id)).status == InvoiceStatus.PAID
        tips = await suggestions.build(s, date.today())
        assert not tips.splits and not tips.small_remainders and len(tips.links) == 1
        try:
            await psync.link_transfer(
                s, await s.get(BankTransfer, s1), await s.get(Invoice, a.id), "admin:web"
            )
            raise AssertionError("linked twice")
        except psync.LinkError as e:
            assert "already resolved" in str(e)


def test_report_periods() -> None:
    today = date(2026, 10, 23)
    assert period_from_arg("", today).start == date(2026, 10, 1)
    assert period_from_arg("last", today).end == date(2026, 9, 30)
    p = period_from_arg("2026-02", today)
    assert (p.start, p.end) == (date(2026, 2, 1), date(2026, 2, 28))
    assert period_from_arg("2026-10", today).end == today
    assert period_from_arg("nonsense", today) is None


async def test_report_command_and_digest(harness: BotHarness, db, settings) -> None:
    inv = await _invoice(db, settings, "1000", "Client Ltd")
    await harness.send_raw(f'/dev_simulate_incoming X 400 "Client Ltd" {inv.number}')
    [msg] = [c.text for c in await harness.send_raw("/report") if isinstance(c, SendMessage)]
    assert "All companies" in msg and "in 400.00" in msg and "Owed to us now <b>600.00</b>" in msg
    assert "1 invoice(s) partly paid" in msg
    assert "Usage" in [c.text for c in await harness.send_raw("/report foo")][-1]

    monday_9 = datetime(2026, 10, 26, 7, 0, tzinfo=UTC)  # 09:00 in Sofia
    sunday = datetime(2026, 10, 25, 7, 0, tzinfo=UTC)
    assert not await maybe_weekly_digest(settings, db, harness.bot, sunday)
    before = len(harness.session.requests)
    assert await maybe_weekly_digest(settings, db, harness.bot, monday_9)
    sent = [c for c in harness.calls_since(before) if isinstance(c, SendMessage)]
    assert [c.chat_id for c in sent] == [1001] and "Weekly summary" in sent[0].text
    assert not await maybe_weekly_digest(settings, db, harness.bot, monday_9)  # once a week
    async with db.session() as s:
        assert (
            await s.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "digest.sent"))
            == 1
        )
