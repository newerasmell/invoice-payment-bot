from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app import reports
from app.db.models import (
    BankTransfer,
    Company,
    Counterparty,
    Invoice,
    MockBankTransfer,
    PaymentMatch,
    Rule,
    RuleRun,
    RuleType,
    TransferDirection,
)
from app.invoices import service as svc
from app.payments import sync as psync
from app.payments.history import HistoryState, load_history
from tests.conftest import CLIENT_DETAILS


def test_periods() -> None:
    today = date(2026, 10, 23)
    assert reports.period_for(None, None, None, today) == reports.Period(date(2026, 10, 1), today)
    assert reports.period_for("last_month", None, None, today) == reports.Period(
        date(2026, 9, 1), date(2026, 9, 30)
    )
    assert reports.period_for("last_30", None, None, today).start == date(2026, 9, 24)
    assert reports.period_for("this_year", None, None, today).start == date(2026, 1, 1)
    custom = reports.period_for(None, "2026-10-20", "2026-10-01", today)
    assert (custom.start, custom.end) == (date(2026, 10, 1), date(2026, 10, 20))
    assert reports.period_for(None, "nonsense", None, today).start == date(2026, 10, 1)


async def _invoice(db, settings, amount: str, due_days_ago: int = -14) -> Invoice:  # noqa: ANN001
    async with db.session() as s:
        x = await s.scalar(
            select(Company).options(selectinload(Company.bank_accounts)).where(Company.code == "X")
        )
        c = Counterparty(name=f"Client {amount}", **CLIENT_DETAILS)
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
    async with db.session() as s:
        inv = await s.get(Invoice, inv.id)
        inv.due_date = date.today() - timedelta(days=due_days_ago)
        await s.commit()
        return inv


async def _pay(db, bank, settings, amount: str, ref: str, who: str = "Someone", out=False):  # noqa: ANN001, ANN201
    t = await bank.simulate(
        direction=TransferDirection.OUTGOING if out else TransferDirection.INCOMING,
        account="41411111111",
        account_holder="Company X Ltd",
        amount=Decimal(amount),
        currency="EUR",
        counterparty_name=who,
        reference=ref,
    )
    return await psync.sync_one(db, bank, t.external_id, settings)


async def test_report_figures(db, bank, settings) -> None:
    a = await _invoice(db, settings, "1000", due_days_ago=40)
    b = await _invoice(db, settings, "300", due_days_ago=-5)
    await _pay(db, bank, settings, "400", f"{a.number} part")
    await _pay(db, bank, settings, "120", "unrelated")
    await _pay(db, bank, settings, "50", "fees", out=True)
    today = date.today()
    period = reports.Period(today - timedelta(days=1), today)
    async with db.session() as s:
        rows, totals = await reports.transfers(s, period)
        assert [r.link for r in rows] == ["unmatched", "review", "invoice"]
        assert [r.signed for r in rows] == [Decimal("-50"), Decimal("120"), Decimal("400")]
        t = totals["EUR"]
        assert (t.incoming, t.outgoing, t.net, t.count_in) == (
            Decimal("520"),
            Decimal("50"),
            Decimal("470"),
            2,
        )
        rec = (await reports.receivables(s, period))["EUR"]
        assert (rec.count, rec.issued, rec.paid, rec.outstanding) == (
            2,
            Decimal("1300.00"),
            Decimal("400.00"),
            Decimal("900.00"),
        )
        summary = await reports.open_invoices(s, today)
        assert [(o.invoice.id, o.bucket) for o in summary.rows] == [
            (a.id, "31–60 days late"),
            (b.id, "Not due"),
        ]
        assert summary.by_bucket["EUR"]["31–60 days late"] == Decimal("600.00")
        assert summary.partial == 1
        empty = reports.Period(today - timedelta(days=60), today - timedelta(days=30))
        assert (await reports.transfers(s, empty))[0] == []


async def test_history_is_recorded_not_acted_on(db, bank, settings) -> None:
    async with db.session() as s:
        x = await s.scalar(select(Company.id).where(Company.code == "X"))
        z = await s.scalar(select(Company.id).where(Company.code == "Z"))
        s.add(
            Rule(
                type=RuleType.INVOICE_ON_RECEIPT,
                trigger_company_id=x,
                target_company_id=z,
                enabled=True,
                params={"percent": "1.5"},
            )
        )
        await s.commit()
    for days_ago in (90, 0):
        await bank.simulate(
            direction=TransferDirection.INCOMING,
            account="41411111111",
            account_holder="Company X Ltd",
            amount=Decimal("1000"),
            currency="EUR",
            counterparty_name="Old Client",
        )
        async with db.session() as s:
            row = await s.scalar(select(MockBankTransfer).order_by(MockBankTransfer.id.desc()))
            row.created = datetime.now(UTC) - timedelta(days=days_ago)
            await s.commit()
    state = HistoryState()
    await load_history(db, bank, settings, date.today() - timedelta(days=120), state)
    assert (state.seen, state.added, state.error, state.running) == (2, 2, None, False)
    async with db.session() as s:
        rows = (await s.scalars(select(BankTransfer).order_by(BankTransfer.booked_at))).all()
        assert rows[0].processed_at is not None  # 90 days old: history only
        assert rows[1].processed_at is None  # recent: left to the poller
        assert await s.scalar(select(func.count(PaymentMatch.id))) == 0
        assert await s.scalar(select(func.count(RuleRun.id))) == 0
    # Loading again adds nothing new.
    await load_history(db, bank, settings, date.today() - timedelta(days=120), state)
    assert (state.seen, state.added) == (2, 0)
