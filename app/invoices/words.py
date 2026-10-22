"""Amount in words for invoices ("Total in words"), English."""

from decimal import Decimal

_ONES = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_SCALES = [(10**12, "trillion"), (10**9, "billion"), (10**6, "million"), (1000, "thousand")]

_CURRENCY = {
    "EUR": ("euro", "euro", "cent", "cents"),
    "USD": ("US dollar", "US dollars", "cent", "cents"),
    "GBP": ("pound sterling", "pounds sterling", "penny", "pence"),
    "BGN": ("lev", "leva", "stotinka", "stotinki"),
    "CHF": ("Swiss franc", "Swiss francs", "centime", "centimes"),
}


def _below_thousand(n: int) -> str:
    parts = []
    if n >= 100:
        parts.append(f"{_ONES[n // 100]} hundred")
        n %= 100
    if n >= 20:
        parts.append(_TENS[n // 10] + (f"-{_ONES[n % 10]}" if n % 10 else ""))
    elif n or not parts:
        parts.append(_ONES[n])
    return " ".join(parts)


def int_to_words(n: int) -> str:
    if n == 0:
        return "zero"
    parts = []
    for value, name in _SCALES:
        if n >= value:
            parts.append(f"{int_to_words(n // value)} {name}")
            n %= value
    if n:
        parts.append(_below_thousand(n))
    return " ".join(parts)


def amount_in_words(amount: Decimal, currency: str) -> str:
    """1619.64 EUR -> 'one thousand six hundred nineteen euro and sixty-four cents'."""
    amount = amount.quantize(Decimal("0.01"))
    if amount < 0:  # credit notes
        return "minus " + amount_in_words(-amount, currency)
    whole = int(amount)
    cents = int((amount - whole) * 100)
    one, many, sub_one, sub_many = _CURRENCY.get(currency, (currency, currency, "cent", "cents"))
    text = f"{int_to_words(whole)} {one if whole == 1 else many}"
    if cents:
        text += f" and {int_to_words(cents)} {sub_one if cents == 1 else sub_many}"
    return text
