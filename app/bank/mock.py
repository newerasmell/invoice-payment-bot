"""Simulated bank for tests and demos. Stores transfers in the app DB (mock_bank_transfers)."""

import secrets
import string
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from app.bank.base import Balance, NotSupported, Transfer
from app.db.models import MockBankTransfer, PaymentInstruction, TransferDirection
from app.db.session import Database
from app.money import to_money

MOCK_OPENING_BALANCE = Decimal("10000.00")


class TransferNotFound(LookupError):
    pass


def _new_transfer_id() -> str:
    # Same shape as Paynetics ids, e.g. OGOVBD3R7BJV.
    return "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(12))


def _to_transfer(row: MockBankTransfer) -> Transfer:
    created = row.created if row.created.tzinfo else row.created.replace(tzinfo=UTC)
    return Transfer(
        external_id=row.transfer_id,
        direction=row.transfer_type,
        status=row.status,
        amount=row.amount,
        currency=row.currency,
        payer_name=row.payer,
        payer_account=row.payer_account,
        beneficiary_name=row.beneficiary,
        beneficiary_account=row.beneficiary_account,
        reference=row.payment_details or None,
        booked_at=created,
        account=row.account,
        raw={
            "transferId": row.transfer_id,
            "transferType": row.transfer_type.value,
            "payer": row.payer,
            "payerAccount": row.payer_account,
            "beneficiary": row.beneficiary,
            "beneficiaryAccount": row.beneficiary_account,
            "amount": f"{row.amount:.2f}",
            "currency": row.currency,
            "paymentDetails": row.payment_details,
            "transferStatus": row.status,
            "created": created.isoformat(),
        },
    )


class MockBankProvider:
    name = "mock"

    def __init__(self, db: Database) -> None:
        self.db = db

    async def get_balance(self, account: str) -> Balance:
        async with self.db.session() as s:
            rows = (
                await s.execute(
                    select(
                        MockBankTransfer.transfer_type,
                        MockBankTransfer.currency,
                        func.sum(MockBankTransfer.amount),
                    )
                    .where(
                        MockBankTransfer.account == account, MockBankTransfer.status == "processed"
                    )
                    .group_by(MockBankTransfer.transfer_type, MockBankTransfer.currency)
                )
            ).all()
        balance = MOCK_OPENING_BALANCE
        currency = "EUR"
        for direction, cur, total in rows:
            # func.sum keeps the Money column type, so `total` is already a Decimal.
            currency = cur
            balance += total if direction == TransferDirection.INCOMING else -total
        balance = to_money(balance)
        return Balance(account=account, currency=currency, balance=balance, available=balance)

    async def list_transfers(
        self, since: datetime, direction: TransferDirection | None = None
    ) -> list[Transfer]:
        q = select(MockBankTransfer).where(MockBankTransfer.created >= since)
        if direction:
            q = q.where(MockBankTransfer.transfer_type == direction)
        async with self.db.session() as s:
            rows = (await s.scalars(q.order_by(MockBankTransfer.created))).all()
        return [_to_transfer(r) for r in rows]

    async def get_transfer(self, transfer_id: str) -> Transfer:
        async with self.db.session() as s:
            row = await s.scalar(
                select(MockBankTransfer).where(MockBankTransfer.transfer_id == transfer_id)
            )
        if row is None:
            raise TransferNotFound(transfer_id)
        return _to_transfer(row)

    async def get_statement(
        self, account: str, date_from: datetime, date_to: datetime
    ) -> list[Transfer]:
        async with self.db.session() as s:
            rows = (
                await s.scalars(
                    select(MockBankTransfer)
                    .where(
                        MockBankTransfer.account == account,
                        MockBankTransfer.created >= date_from,
                        MockBankTransfer.created < date_to + timedelta(days=1),
                    )
                    .order_by(MockBankTransfer.created)
                )
            ).all()
        return [_to_transfer(r) for r in rows]

    async def create_transfer(self, instr: PaymentInstruction) -> None:
        raise NotSupported("transfers are executed manually in the bank panel")

    # --- simulation helpers (used by /dev_* commands and tests) ---

    async def simulate(
        self,
        *,
        direction: TransferDirection,
        account: str,
        account_holder: str,
        amount: Decimal,
        currency: str,
        counterparty_name: str,
        counterparty_account: str | None = None,
        reference: str = "",
        status: str = "processed",
    ) -> Transfer:
        if direction == TransferDirection.INCOMING:
            payer, payer_acc = counterparty_name, counterparty_account
            benef, benef_acc = account_holder, account
        else:
            payer, payer_acc = account_holder, account
            benef, benef_acc = counterparty_name, counterparty_account
        row = MockBankTransfer(
            transfer_id=_new_transfer_id(),
            transfer_type=direction,
            account=account,
            payer=payer,
            payer_account=payer_acc,
            beneficiary=benef,
            beneficiary_account=benef_acc,
            amount=to_money(amount),
            currency=currency,
            payment_details=reference,
            status=status,
            created=datetime.now(UTC),
        )
        async with self.db.session() as s:
            s.add(row)
            await s.commit()
        return _to_transfer(row)
