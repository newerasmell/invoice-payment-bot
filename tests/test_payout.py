from datetime import UTC, datetime
from decimal import Decimal

import pytest
from aiogram.methods import SendDocument, SendMessage
from sqlalchemy import select

from app.bot.documents_flow import Ins
from app.bot.invoice_flow import Inv
from app.companies import counterparty_for_company
from app.db.models import (
    Company,
    Contract,
    InstructionStatus,
    Invoice,
    InvoiceStatus,
    PaymentInstruction,
    Rule,
    RuleRun,
    RuleType,
    TransferDirection,
)
from app.payments import sync as psync
from app.rules.engine import payout_amount, render_reference
from tests.conftest import BotHarness


@pytest.mark.parametrize(
    ("mode", "value", "received", "expected"),
    [
        ("percent", "100", "9850.00", "9850.00"),
        ("percent", "33.333", "100.00", "33.33"),
        ("percent", "50", "0.03", "0.02"),  # 0.015 half-up
        ("fixed", "500", "9850.00", "500.00"),
        ("fixed", "20000", "9850.00", "9850.00"),  # never more than received
        ("remainder", "150", "9850.00", "9700.00"),
        ("remainder", "10000", "9850.00", "-150.00"),  # caller rejects non-positive
    ],
)
def test_payout_amount(mode: str, value: str, received: str, expected: str) -> None:
    assert payout_amount(Decimal(received), mode, Decimal(value)) == Decimal(expected)


def test_reference_template() -> None:
    ref = render_reference(
        "Payout {date} from {payer} {amount} {currency} ({reference})",
        payer="Company X Ltd",
        received=Decimal("9850"),
        currency="EUR",
        transfer_ref="INV 0000000001",
        booked="22.10.2026",
    )
    assert ref == "Payout 22.10.2026 from Company X Ltd 9850.00 EUR (INV 0000000001)"


async def _ids(db) -> dict[str, int]:  # noqa: ANN001
    async with db.session() as s:
        return {c.code: c.id for c in (await s.scalars(select(Company))).all()}


async def _payout_rule(db, **params) -> int:  # noqa: ANN001, ANN003
    ids = await _ids(db)
    async with db.session() as s:
        r = Rule(
            type=RuleType.PAYOUT_ON_RECEIPT,
            trigger_company_id=ids["Z"],
            target_company_id=ids["G"],
            enabled=True,
            params={
                "mode": "percent",
                "value": "100",
                "reference_template": "Payout {date}",
                **params,
            },
        )
        s.add(r)
        await s.commit()
        return r.id


async def _pay_z(bank, db, settings, amount: str, payer: str, payer_acc: str | None):  # noqa: ANN001, ANN201
    t = await bank.simulate(
        direction=TransferDirection.INCOMING,
        account="41422222222",
        account_holder="Company Z Ltd",
        amount=Decimal(amount),
        currency="EUR",
        counterparty_name=payer,
        counterparty_account=payer_acc,
    )
    return await psync.sync_one(db, bank, t.external_id, settings)


async def test_payout_rule_creates_instruction_to_g(db, bank, settings) -> None:
    await _payout_rule(db, mode="remainder", value="150")
    events = await _pay_z(bank, db, settings, "9850.00", "Company X Ltd", "BG86PAYN96611000000001")
    [ev] = [e for e in events if e.kind == "rule_instruction"]
    async with db.session() as s:
        i = await s.get(PaymentInstruction, ev.instruction_id)
        g = await s.scalar(select(Company).where(Company.code == "G"))
        assert (i.to_iban, i.to_name, i.amount) == (
            "BG32PAYN96611000000003",
            g.legal_name,
            Decimal("9700.00"),
        )
        assert i.status == InstructionStatus.PROPOSED and i.reference.startswith("Payout ")
        assert (await s.scalar(select(RuleRun))).instruction_id == i.id
    # Once per payment: processing the same transfer again proposes nothing new.
    [t] = await bank.list_transfers(datetime(2000, 1, 1, tzinfo=UTC))
    assert await psync.sync_one(db, bank, t.external_id, settings) == []


async def test_from_company_filter_and_external_iban(db, bank, settings) -> None:
    ids = await _ids(db)
    await _payout_rule(
        db,
        from_company_id=ids["X"],
        iban="DE89370400440532013000",
        name="Partner GmbH",
        mode="fixed",
        value="250",
    )
    events = await _pay_z(bank, db, settings, "1000.00", "Random Client", "BG80BNBG96611020345678")
    assert [e for e in events if e.kind == "rule_instruction"] == []
    events = await _pay_z(bank, db, settings, "1000.00", "Company X Ltd", None)
    [ev] = [e for e in events if e.kind == "rule_instruction"]
    async with db.session() as s:
        i = await s.get(PaymentInstruction, ev.instruction_id)
        assert (i.to_iban, i.to_name, i.amount) == (
            "DE89370400440532013000",
            "Partner GmbH",
            Decimal("250.00"),
        )
        runs = (await s.scalars(select(RuleRun).order_by(RuleRun.id))).all()
        assert [r.status for r in runs] == ["skipped", "drafted"]


async def test_payout_failure_recorded(db, bank, settings) -> None:
    await _payout_rule(db, mode="remainder", value="5000")
    events = await _pay_z(bank, db, settings, "1000.00", "Company X Ltd", None)
    assert [e for e in events if e.kind == "rule_instruction"] == []
    async with db.session() as s:
        run = await s.scalar(select(RuleRun))
        assert run.status == "failed" and "not positive" in run.detail


async def test_full_cycle_x_z_g(harness: BotHarness, db, settings) -> None:
    """The brief's M5 'done when': client pays X -> Z invoices X (Y - 1.5%) -> X pays Z ->
    invoice PAID -> payout instruction Z -> G -> executed. Mock bank end to end."""
    ids = await _ids(db)
    async with db.session() as s:
        x = await s.get(Company, ids["X"])
        z = await s.get(Company, ids["Z"])
        z.default_vat_rate = Decimal("0")
        cp = await counterparty_for_company(s, x)
        c = Contract(
            company_id=z.id,
            counterparty_id=cp.id,
            service_description="Marketing",
            payment_terms_days=10,
        )
        s.add(c)
        await s.flush()
        s.add(
            Rule(
                type=RuleType.INVOICE_ON_RECEIPT,
                trigger_company_id=x.id,
                target_company_id=z.id,
                enabled=True,
                params={"percent": "1.5", "contract_id": c.id},
            )
        )
        s.add(
            Rule(
                type=RuleType.PAYOUT_ON_RECEIPT,
                trigger_company_id=z.id,
                target_company_id=ids["G"],
                enabled=True,
                params={
                    "mode": "percent",
                    "value": "100",
                    "from_company_id": x.id,
                    "reference_template": "Payout {reference}",
                },
            )
        )
        await s.commit()

    # 1. A client pays X 10,000 -> rule drafts Z -> X for 9,850.
    calls = await harness.send_raw('/dev_simulate_incoming X 10000 "Client Ltd" order 77')
    [draft] = [c for c in calls if isinstance(c, SendDocument)]
    inv_id = int(draft.reply_markup.inline_keyboard[0][0].callback_data.rsplit(":", 1)[1])
    await harness.click(Inv(action="ap", id=inv_id).pack())
    async with db.session() as s:
        inv = await s.get(Invoice, inv_id)
        number = inv.number
        assert inv.amount_gross == Decimal("9850.00") and inv.status == InvoiceStatus.APPROVED

    # 2. X pays Z's invoice (seen as incoming at Z, with the invoice number in the reason).
    calls = await harness.send_raw(f'/dev_simulate_incoming Z 9850 "Company X Ltd" {number}')
    texts = [c.text for c in calls if isinstance(c, SendMessage)]
    assert any("marked <b>PAID</b>" in t and number in t for t in texts)
    [card] = [c for c in calls if isinstance(c, SendMessage) and "Payout rule" in c.text]
    assert "From: <b>Z</b>" in card.text and "BG32PAYN96611000000003" in card.text
    assert "<code>9850.00</code>" in card.text and f"Payout {number}" in card.text
    async with db.session() as s:
        assert (await s.get(Invoice, inv_id)).status == InvoiceStatus.PAID
        instr = await s.scalar(select(PaymentInstruction))

    # 3. Approve internally, pay in the bank, polling marks it executed.
    await harness.click(Ins(action="ap", id=instr.id).pack())
    calls = await harness.send_raw(
        f'/dev_simulate_outgoing Z 9850 "Company G Ltd" BG32PAYN96611000000003 Payout {number}'
    )
    assert any("Payment executed" in (getattr(c, "text", "") or "") for c in calls)
    async with db.session() as s:
        assert (await s.get(PaymentInstruction, instr.id)).status == InstructionStatus.EXECUTED
    listing = [c.text for c in await harness.send_raw("/rules") if isinstance(c, SendMessage)][0]
    assert "pay <b>G</b> 100% of it" in listing
