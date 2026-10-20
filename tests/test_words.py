from decimal import Decimal

import pytest

from app.invoices.render import _tint
from app.invoices.words import amount_in_words


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [
        ("1619.64", "EUR", "one thousand six hundred nineteen euro and sixty-four cents"),
        ("12600", "EUR", "twelve thousand six hundred euro"),
        ("1", "USD", "one US dollar"),
        ("0.01", "GBP", "zero pounds sterling and one penny"),
        ("2000015.10", "BGN", "two million fifteen leva and ten stotinki"),
        ("100", "XYZ", "one hundred XYZ"),
    ],
)
def test_amount_in_words(amount: str, currency: str, expected: str) -> None:
    assert amount_in_words(Decimal(amount), currency) == expected


def test_tint() -> None:
    assert _tint("#000000", 0.5) == "#808080"
    assert _tint("#1F3A5F", 0) == "#1F3A5F" and _tint("#1F3A5F", 1) == "#FFFFFF"
