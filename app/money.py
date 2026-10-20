"""Money helpers. Amounts are always Decimal, rounded half-up to 2 decimals."""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")


class InvalidAmount(ValueError):
    pass


def to_money(value: str | int | Decimal) -> Decimal:
    """Parse a user- or API-supplied amount into a 2-decimal Decimal.

    Accepts "1200", "1200.5", "1 200,50" and Decimal/int. Floats are rejected on purpose.
    """
    if isinstance(value, float):
        raise TypeError("floats are not accepted for money; pass a str or Decimal")
    if isinstance(value, str):
        cleaned = value.strip().replace(" ", "").replace(" ", "")
        if "," in cleaned and "." not in cleaned:
            cleaned = cleaned.replace(",", ".")
        try:
            value = Decimal(cleaned)
        except InvalidOperation as e:
            raise InvalidAmount(f"not a valid amount: {value!r}") from e
    d = Decimal(value)
    if not d.is_finite():
        raise InvalidAmount(f"not a valid amount: {value!r}")
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def to_cents(amount: Decimal) -> int:
    return int(to_money(amount) * 100)


def from_cents(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(CENT)


def fmt_money(amount: Decimal, currency: str) -> str:
    return f"{to_money(amount):,.2f} {currency}"
