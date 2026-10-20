from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from pydantic import BaseModel

from app.db.models import PaymentInstruction, TransferDirection


class NotSupported(Exception):
    """The bank cannot do this via API; the flow falls back to manual execution."""


class Balance(BaseModel):
    account: str
    name: str | None = None
    currency: str
    balance: Decimal
    available: Decimal


class Transfer(BaseModel):
    """A transfer as reported by the bank, normalised to the Paynetics transfer shape."""

    external_id: str
    direction: TransferDirection
    status: str  # new | pending | processed | canceled | reversed
    amount: Decimal
    currency: str
    payer_name: str | None = None
    payer_account: str | None = None
    beneficiary_name: str | None = None
    beneficiary_account: str | None = None
    reference: str | None = None
    booked_at: datetime | None = None
    raw: dict[str, Any] = {}


class BankProvider(Protocol):
    name: str

    async def get_balance(self, account: str) -> Balance: ...

    async def list_transfers(
        self, since: datetime, direction: TransferDirection | None = None
    ) -> list[Transfer]: ...

    async def get_transfer(self, transfer_id: str) -> Transfer: ...

    async def get_statement(
        self, account: str, date_from: datetime, date_to: datetime
    ) -> list[Transfer]: ...

    async def create_transfer(self, instr: PaymentInstruction) -> None:
        """Not supported by Paynetics today -> raise NotSupported (manual execution)."""
        ...
