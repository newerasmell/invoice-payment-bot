from decimal import Decimal

from sqlalchemy import BigInteger
from sqlalchemy.types import TypeDecorator

from app.money import from_cents, to_cents


class Money(TypeDecorator[Decimal]):
    """Stores a Decimal amount as integer cents, so SQLite never sees a float."""

    impl = BigInteger
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, dialect: object) -> int | None:
        return None if value is None else to_cents(value)

    def process_result_value(self, value: int | None, dialect: object) -> Decimal | None:
        return None if value is None else from_cents(value)
