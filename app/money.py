"""Money helpers. Amounts are always Decimal, rounded half-up to 2 decimals."""

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")


class InvalidAmount(ValueError):
    pass


_CURRENCY_MARKS = re.compile(r"(€|\$|£|EUR|USD|GBP|BGN|лв\.?)", re.I)


def _normalize(raw: str) -> str:
    """'76,908.00' / '76.908,00' / '12,600' / '1 200,50' / '€ 60' -> plain '1234.56' form."""
    s = _CURRENCY_MARKS.sub("", raw.strip())
    s = re.sub(r"[\s\u00a0\u202f']", "", s)
    if "," in s and "." in s:
        # The later separator is the decimal one.
        if s.rfind(",") > s.rfind("."):
            return s.replace(".", "").replace(",", ".")
        return s.replace(",", "")
    if "," in s:
        # "12,600" / "1,234,567" are thousands; "1200,5" / "1 200,50" use a decimal comma.
        if re.fullmatch(r"-?\d{1,3}(,\d{3})+", s):
            return s.replace(",", "")
        return s.replace(",", ".")
    return s


_SHORTHAND = re.compile(r"^(-?[\d.,\s]+?)\s*(k|m|mn|mln)\.?$", re.I)
_MULTIPLIER = {"k": 1000, "m": 1_000_000, "mn": 1_000_000, "mln": 1_000_000}


def _expand_shorthand(raw: str) -> str | None:
    """ "36k" -> "36000", "1.5m" -> "1500000" (people write amounts like that in chat)."""
    m = _SHORTHAND.match(_CURRENCY_MARKS.sub("", raw.strip()).strip())
    if not m:
        return None
    try:
        number = Decimal(_normalize(m.group(1)))
    except InvalidOperation:
        return None
    return str(number * _MULTIPLIER[m.group(2).lower()])


def to_money(value: str | int | Decimal) -> Decimal:
    """Parse a user- or API-supplied amount into a 2-decimal Decimal.

    Accepts "1200", "1200.5", "1 200,50" and Decimal/int. Floats are rejected on purpose.
    """
    if isinstance(value, float):
        raise TypeError("floats are not accepted for money; pass a str or Decimal")
    if isinstance(value, str):
        cleaned = _expand_shorthand(value) or _normalize(value)
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
