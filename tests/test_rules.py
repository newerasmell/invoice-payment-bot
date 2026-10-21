from decimal import Decimal

import pytest
from aiogram.methods import SendDocument, SendMessage
from sqlalchemy import func, select

from app.bot.invoice_flow import Inv
from app.companies import counterparty_for_company
from app.db.models import (
    Company,
    Contract,
    Invoice,
    InvoiceSource,
    InvoiceStatus,
    Rule,
    RuleRun,
    RuleType,
    TransferDirection,
)
from app.payments import sync as psync
from app.rules.engine import amount_after_fee
from tests.conftest import BotHarness


@pytest.mark.parametrize(
    ("received", "percent", "expected"),
    [
        ("10000.00", "1.5", "9850.00"),
        ("1234.56", "1.5", "1216.04"),  # 1216.0416
        ("100.33", "1.5", "98.83"),  # 98.82505 -> half-up
        ("98000.00", "1.5", "96530.00"),
        ("0.01", "1.5", "0.01"),
        ("500.00", "0", "500.00"),
        ("200.00", "2.25", "195.50"),
    ],
)
def test_amount_after_fee(received: str, percent: str, expected: str) -> None:
    assert amount_after_fee(Decimal(received), Decimal(percent)) == Decimal(expected)


async def _rule(db, percent: str = "1.5", vat_zero: bool = True, **params) -> int:  # noqa: ANN001, ANN003
    async with db.session() as s:
        x = await s.scalar(select(Company).where(Company.code == "X"))
        z = await s.scalar(select(Company).where(Company.code == "Z"))
        if vat_zero:
            z.default_vat_rate = Decimal("0")
        cp = await counterparty_for_company(s, x)
        c = Contract(
            company_id=z.id,
            counterparty_id=cp.id,
            service_description="Marketing services under agreement 12/2026",
            payment_terms_days=10,
        )
        s.add(c)
        await s.flush()
        r = Rule(
            type=RuleType.INVOICE_ON_RECEIPT,
            trigger_company_id=x.id,
            target_company_id=z.id,
            enabled=True,
            params={"percent": percent, "contract_id": c.id, **params},
        )
        s.add(r)
        await s.commit()
        return r.id


async def _pay(
    bank,
    settings,
    db,
    amount: str,
    payer: str = "Client Ltd",  # noqa: ANN001
    payer_acc: str | None = "BG80BNBG96611020345678",
    account: str = "41411111111",
):
    t = await bank.simulate(
        direction=TransferDirection.INCOMING,
        account=account,
        account_holder="Company X Ltd",
        amount=Decimal(amount),
        currency="EUR",
        counterparty_name=payer,
        counterparty_account=payer_acc,
    )
    return await psync.sync_one(db, bank, t.external_id, settings)


async def test_rule_drafts_invoice_from_z_to_x(db, bank, settings) -> None:
    await _rule(db)
    events = await _pay(bank, settings, db, "10000.00")
    [draft] = [e for e in events if e.kind == "rule_draft"]
    assert draft.rule_draft.amount == Decimal("9850.00")
    async with db.session() as s:
        inv = await s.get(Invoice, draft.rule_draft.invoice_id)
        z = await s.get(Company, inv.issuer_id)
        client = await counterparty_for_company(
            s, await s.scalar(select(Company).where(Company.code == "X"))
        )
        lines = (await s.execute(select(Invoice).where(Invoice.id == inv.id))).scalar_one()
        assert z.code == "Z" and inv.counterparty_id == client.id and client.company_id
        assert inv.status == InvoiceStatus.DRAFT and inv.source == InvoiceSource.RULE
        assert inv.amount_gross == Decimal("9850.00") and lines.currency == "EUR"
        assert (inv.due_date - inv.issue_date).days == 10
        run = await s.scalar(select(RuleRun))
        assert run.status == "drafted" and run.invoice_id == inv.id

    # Idempotent: the same payment never drafts twice.
    async with db.session() as s:
        from app.db.models import BankTransfer

        bt = await s.scalar(select(BankTransfer))
        bt.processed_at = None  # even if processing were somehow repeated
        await s.commit()
    from app.rules.engine import run_receipt_rules

    async with db.session() as s:
        bt = await s.scalar(select(BankTransfer))
        assert await run_receipt_rules(s, settings, bt) == []
        assert await s.scalar(select(func.count(RuleRun.id))) == 1


async def test_gross_equals_amount_with_vat(db, bank, settings) -> None:
    await _rule(db, vat_zero=False)  # Z has 20% VAT in the example profile
    [draft] = [e for e in await _pay(bank, settings, db, "10000.00") if e.kind == "rule_draft"]
    async with db.session() as s:
        inv = await s.get(Invoice, draft.rule_draft.invoice_id)
        assert inv.amount_gross == Decimal("9850.00") and inv.amount_vat == Decimal("1641.67")


async def test_payments_from_our_companies_do_not_trigger(db, bank, settings) -> None:
    await _rule(db)
    events = await _pay(
        bank, settings, db, "500.00", payer="Somebody", payer_acc="BG59PAYN96611000000002"
    )  # Z's IBAN
    assert [e for e in events if e.kind == "rule_draft"] == []
    events = await _pay(bank, settings, db, "600.00", payer="Company G Ltd", payer_acc=None)
    assert [e for e in events if e.kind == "rule_draft"] == []
    async with db.session() as s:
        runs = (await s.scalars(select(RuleRun))).all()
        assert [r.status for r in runs] == ["skipped", "skipped"]


async def test_include_internal_and_disabled(db, bank, settings) -> None:
    rid = await _rule(db, include_internal=True)
    events = await _pay(bank, settings, db, "500.00", payer="Company G Ltd", payer_acc=None)
    assert len([e for e in events if e.kind == "rule_draft"]) == 1
    async with db.session() as s:
        (await s.get(Rule, rid)).enabled = False
        await s.commit()
    assert [e for e in await _pay(bank, settings, db, "700.00") if e.kind == "rule_draft"] == []


async def test_limit_failure_is_recorded(db, bank, settings) -> None:
    await _rule(db)
    events = await _pay(bank, settings, db, "60000.00")
    assert [e for e in events if e.kind == "rule_draft"] == []
    async with db.session() as s:
        run = await s.scalar(select(RuleRun))
        assert run.status == "failed" and "above the limit" in run.detail


async def test_bot_flow_rule_draft_then_approve(harness: BotHarness, db) -> None:
    await _rule(db)
    calls = await harness.send_raw('/dev_simulate_incoming X 10000 "Client Ltd" order 77')
    [doc] = [c for c in calls if isinstance(c, SendDocument)]
    assert "Rule</b>: X received 10,000.00 EUR → Z invoices X 9,850.00 EUR" in doc.caption
    assert "Z → Company X Ltd" in doc.caption
    data = [b.callback_data for row in doc.reply_markup.inline_keyboard for b in row]
    inv_id = int(data[0].rsplit(":", 1)[1])
    assert data[0] == Inv(action="ap", id=inv_id).pack()
    calls = await harness.click(data[0])
    [final] = [c for c in calls if isinstance(c, SendDocument)]
    assert final.document.filename == "invoice-0000000001.pdf"

    [listing] = [c.text for c in await harness.send_raw("/rules") if isinstance(c, SendMessage)]
    assert "When <b>X</b> receives a payment → <b>Z</b>" in listing and "−1.5%" in listing
