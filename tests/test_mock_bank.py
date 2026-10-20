from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.bank.base import NotSupported
from app.bank.mock import MOCK_OPENING_BALANCE, MockBankProvider, TransferNotFound
from app.db.models import PaymentInstruction, TransferDirection

ACC = "41411111111"


async def _in(bank: MockBankProvider, amount: str, ref: str = "") -> object:
    return await bank.simulate(
        direction=TransferDirection.INCOMING,
        account=ACC,
        account_holder="Company X Ltd",
        amount=Decimal(amount),
        currency="EUR",
        counterparty_name="Client Ltd",
        counterparty_account="BG11CLNT00000000000001",
        reference=ref,
    )


async def test_balance_moves_with_transfers(bank: MockBankProvider) -> None:
    assert (await bank.get_balance(ACC)).balance == MOCK_OPENING_BALANCE
    await _in(bank, "1200.50")
    await bank.simulate(
        direction=TransferDirection.OUTGOING,
        account=ACC,
        account_holder="Company X Ltd",
        amount=Decimal("200.25"),
        currency="EUR",
        counterparty_name="Company Z Ltd",
    )
    bal = await bank.get_balance(ACC)
    assert bal.balance == MOCK_OPENING_BALANCE + Decimal("1000.25")
    assert bal.currency == "EUR"
    # Other accounts are unaffected.
    assert (await bank.get_balance("41422222222")).balance == MOCK_OPENING_BALANCE


async def test_transfer_shape_and_lookup(bank: MockBankProvider) -> None:
    t = await _in(bank, "99.99", "INV 0000000001")
    assert len(t.external_id) == 12
    assert t.payer_name == "Client Ltd" and t.beneficiary_account == ACC
    assert t.raw["amount"] == "99.99" and t.raw["paymentDetails"] == "INV 0000000001"
    assert (await bank.get_transfer(t.external_id)) == t
    with pytest.raises(TransferNotFound):
        await bank.get_transfer("NOPE")


async def test_list_filters(bank: MockBankProvider) -> None:
    await _in(bank, "1")
    await bank.simulate(
        direction=TransferDirection.OUTGOING,
        account=ACC,
        account_holder="X",
        amount=Decimal("2"),
        currency="EUR",
        counterparty_name="Z",
    )
    since = datetime.now(UTC) - timedelta(minutes=5)
    assert len(await bank.list_transfers(since)) == 2
    incoming = await bank.list_transfers(since, TransferDirection.INCOMING)
    assert [t.amount for t in incoming] == [Decimal("1.00")]
    assert await bank.list_transfers(datetime.now(UTC) + timedelta(minutes=1)) == []


async def test_create_transfer_not_supported(bank: MockBankProvider) -> None:
    with pytest.raises(NotSupported):
        await bank.create_transfer(PaymentInstruction())
