from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from aiogram.methods import SendMessage
from sqlalchemy import select

from app.db.models import (
    Company,
    Counterparty,
    InstructionStatus,
    Invoice,
    InvoiceStatus,
    PaymentInstruction,
)
from app.reminders import collect, send
from tests.conftest import BotHarness


async def _invoice(db, status: InvoiceStatus, due: date, number: str = "0000000007") -> int:  # noqa: ANN001
    async with db.session() as s:
        z = await s.scalar(select(Company).where(Company.code == "Z"))
        cp = Counterparty(name="Late Payer Ltd")
        s.add(cp)
        await s.flush()
        inv = Invoice(
            number=number,
            issuer_id=z.id,
            counterparty_id=cp.id,
            amount_net=Decimal("100"),
            vat_rate=Decimal("0"),
            amount_vat=Decimal("0"),
            amount_gross=Decimal("100"),
            currency="EUR",
            issue_date=due - timedelta(days=14),
            due_date=due,
            status=status,
        )
        s.add(inv)
        await s.commit()
        return inv.id


async def _instruction(
    db, approved_hours_ago: float, status=InstructionStatus.AWAITING_BANK
) -> int:  # noqa: ANN001
    async with db.session() as s:
        x = await s.scalar(select(Company).where(Company.code == "X"))
        i = PaymentInstruction(
            from_company_id=x.id,
            to_iban="BG80BNBG96611020345678",
            to_name="Supplier Ltd",
            amount=Decimal("250"),
            currency="EUR",
            reference="INV 9",
            status=status,
            approved_by=1001,
            approved_at=datetime.now(UTC) - timedelta(hours=approved_hours_ago),
        )
        s.add(i)
        await s.commit()
        return i.id


async def test_overdue_invoice_reminded_once_a_day(db, settings) -> None:
    today = date.today()
    await _invoice(db, InvoiceStatus.SENT, today - timedelta(days=3))
    await _invoice(db, InvoiceStatus.SENT, today, number="0000000008")  # due today: not yet
    await _invoice(db, InvoiceStatus.PAID, today - timedelta(days=9), number="0000000009")
    await _invoice(db, InvoiceStatus.DRAFT, today - timedelta(days=9), number="0000000010")
    now = datetime.now(UTC)
    async with db.session() as s:
        [r] = await collect(s, settings, now)
        await s.commit()
    assert r.kind == "invoice_overdue" and "0000000007" in r.text and "overdue by 3 days" in r.text
    async with db.session() as s:
        assert await collect(s, settings, now + timedelta(hours=5)) == []
        await s.commit()
    async with db.session() as s:
        later = await collect(s, settings, now + timedelta(hours=25))
    # The next day: reminded again, and the one due "today" is now overdue too.
    assert sorted(r.text[13:23] for r in later) == ["0000000007", "0000000008"]


async def test_instruction_waiting_for_bank(db, settings) -> None:
    await _instruction(db, 10)  # too recent
    late = await _instruction(db, 50)
    await _instruction(db, 80, InstructionStatus.EXECUTED)
    async with db.session() as s:
        [r] = await collect(s, settings)
        await s.commit()
    assert (r.kind, r.entity_id) == ("instruction_waiting", late)
    assert "approved 50 h ago" in r.text and "Paynetics panel of X" in r.text


async def test_send_to_users_of_the_company(harness: BotHarness, db, settings) -> None:
    from app.db.models import AllowedUser, UserRole

    async with db.session() as s:
        s.add(AllowedUser(telegram_id=2005, role=UserRole.OPERATOR, companies=["G"]))
        s.add(AllowedUser(telegram_id=2006, role=UserRole.OPERATOR, companies=["X"]))
        await s.commit()
    await _instruction(db, 50)
    async with db.session() as s:
        reminders = await collect(s, settings)
        await s.commit()
    before = len(harness.session.requests)
    await send(harness.bot, db, reminders)
    sent = [c for c in harness.calls_since(before) if isinstance(c, SendMessage)]
    assert sorted(c.chat_id for c in sent) == [1001, 2006]
