from decimal import Decimal

import pytest

from app.money import InvalidAmount, fmt_money, from_cents, to_cents, to_money
from app.redact import redact_iban


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1200", "1200.00"),
        ("1200.5", "1200.50"),
        ("1 200,50", "1200.50"),
        ("0.005", "0.01"),  # half-up, not banker's rounding
        ("0.015", "0.02"),
        ("2.675", "2.68"),
        (Decimal("10"), "10.00"),
        (7, "7.00"),
    ],
)
def test_to_money(raw: object, expected: str) -> None:
    assert to_money(raw) == Decimal(expected)  # type: ignore[arg-type]


def test_float_rejected() -> None:
    with pytest.raises(TypeError):
        to_money(1.5)  # type: ignore[arg-type]


@pytest.mark.parametrize("raw", ["abc", "", "NaN", "Infinity"])
def test_invalid(raw: str) -> None:
    with pytest.raises(InvalidAmount):
        to_money(raw)


def test_cents_roundtrip() -> None:
    assert to_cents(Decimal("1234.56")) == 123456
    assert from_cents(123456) == Decimal("1234.56")


def test_fmt() -> None:
    assert fmt_money(Decimal("1234.5"), "EUR") == "1,234.50 EUR"


def test_redact_iban() -> None:
    assert redact_iban("BG80 BNBG 9661 1020 3456 78") == "BG**...5678"
    assert redact_iban(None) == ""
