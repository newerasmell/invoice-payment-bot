from decimal import Decimal

from sqlalchemy import select

from app.bank.mock import MOCK_OPENING_BALANCE
from app.bot.setup import make_dispatcher
from app.config import BankProviderName
from app.db.models import AuditLog, MockBankTransfer
from tests.conftest import STRANGER_ID, BotHarness


async def test_stranger_is_ignored_silently(harness: BotHarness) -> None:
    assert await harness.send("/start", user_id=STRANGER_ID) == []
    assert await harness.send("/dev_simulate_incoming X 100 Evil", user_id=STRANGER_ID) == []
    assert harness.session.requests == []


async def test_start_and_help(harness: BotHarness) -> None:
    [reply] = await harness.send("/start")
    assert "approver" in reply
    [reply] = await harness.send("/help")
    assert "/companies" in reply and "/dev_simulate_incoming" in reply


async def test_companies_lists_profiles_with_redacted_iban(harness: BotHarness) -> None:
    [reply] = await harness.send("/companies")
    assert "Company X Ltd" in reply and "Company Z Ltd" in reply
    assert "BG**...0001" in reply
    assert "BG86PAYN96611000000001" not in reply


async def test_balance(harness: BotHarness) -> None:
    [reply] = await harness.send("/balance x")
    assert "10,000.00 EUR" in reply
    [reply] = await harness.send("/balance Q")
    assert "Unknown company" in reply and "G, X, Z" in reply
    [reply] = await harness.send("/balance")
    assert "Usage" in reply


async def test_simulate_incoming_changes_balance_and_audits(harness: BotHarness, db) -> None:
    [reply] = await harness.send('/dev_simulate_incoming X 1200,50 "Client Ltd" INV 0000000001')
    assert "[MOCK] Incoming" in reply and "1,200.50 EUR" in reply
    assert "Client Ltd" in reply and "INV 0000000001" in reply

    [reply] = await harness.send("/balance X")
    expected = MOCK_OPENING_BALANCE + Decimal("1200.50")
    assert f"{expected:,.2f} EUR" in reply

    async with db.session() as s:
        row = await s.scalar(select(MockBankTransfer))
        log = await s.scalar(select(AuditLog).where(AuditLog.action == "dev.simulate_incoming"))
    assert row.beneficiary == "Company X Ltd" and row.payment_details == "INV 0000000001"
    assert log.actor == "tg:1001" and log.payload["amount"] == "1200.50"


async def test_simulate_outgoing_with_iban(harness: BotHarness, db) -> None:
    [reply] = await harness.send(
        '/dev_simulate_outgoing Z 300 "Company G Ltd" BG32PAYN96611000000003 payout oct'
    )
    assert "[MOCK] Outgoing" in reply and "payout oct" in reply
    async with db.session() as s:
        row = await s.scalar(select(MockBankTransfer))
    assert row.beneficiary_account == "BG32PAYN96611000000003"
    assert row.payer == "Company Z Ltd"


async def test_simulate_validation(harness: BotHarness) -> None:
    assert "Usage" in (await harness.send("/dev_simulate_incoming X 100"))[0]
    assert "Invalid amount" in (await harness.send("/dev_simulate_incoming X abc Bob"))[0]
    assert "Invalid amount" in (await harness.send("/dev_simulate_incoming X -5 Bob"))[0]
    assert "Unknown company" in (await harness.send("/dev_simulate_incoming Q 5 Bob"))[0]
    assert "Could not parse" in (await harness.send('/dev_simulate_incoming X 5 "Bob'))[0]


async def test_dev_commands_disabled_outside_mock(harness: BotHarness, settings, db, bank) -> None:
    prod = settings.model_copy(update={"bank_provider": BankProviderName.PAYNETICS})
    harness.dp = make_dispatcher(prod, db, bank)
    [reply] = await harness.send("/dev_simulate_incoming X 100 Bob")
    assert "didn't understand" in reply
    [reply] = await harness.send("/help")
    assert "dev_simulate" not in reply
