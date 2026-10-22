"""Small helpers for parsing and validating admin form posts."""

import re
from decimal import Decimal, InvalidOperation

from starlette.datastructures import FormData

from app.iban import iban_is_valid, normalize_iban

__all__ = ["iban_is_valid", "normalize_iban"]


class FormErrors(dict[str, str]):
    pass


def text(form: FormData, key: str) -> str | None:
    v = form.get(key)
    if not isinstance(v, str):
        return None
    v = v.strip()
    return v or None


def checkbox(form: FormData, key: str) -> bool:
    return form.get(key) in ("on", "true", "1")


def integer(form: FormData, key: str, errors: FormErrors, *, minimum: int = 0) -> int | None:
    raw = text(form, key)
    if raw is None:
        errors[key] = "Required"
        return None
    try:
        v = int(raw)
    except ValueError:
        errors[key] = "Must be a whole number"
        return None
    if v < minimum:
        errors[key] = f"Must be at least {minimum}"
        return None
    return v


def percent(form: FormData, key: str, errors: FormErrors) -> Decimal | None:
    raw = text(form, key)
    if raw is None:
        errors[key] = "Required"
        return None
    try:
        v = Decimal(raw.replace(",", "."))
    except InvalidOperation:
        errors[key] = "Must be a number"
        return None
    if not (0 <= v <= 100):
        errors[key] = "Must be between 0 and 100"
        return None
    return v.quantize(Decimal("0.01"))


EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CODE = re.compile(r"^[A-Z0-9_-]{1,16}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_BIC = re.compile(r"^[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?$")


def company_code(raw: str | None, errors: FormErrors) -> str | None:
    if raw is None:
        errors["code"] = "Required"
        return None
    v = raw.upper()
    if not _CODE.match(v):
        errors["code"] = "1-16 characters: letters, digits, - or _"
        return None
    return v


def currency(raw: str | None, errors: FormErrors, key: str = "currency") -> str | None:
    v = (raw or "").upper()
    if not _CURRENCY.match(v):
        errors[key] = "3-letter currency code, e.g. EUR"
        return None
    return v


def iban(raw: str | None, errors: FormErrors) -> str | None:
    if raw is None:
        errors["iban"] = "Required"
        return None
    v = normalize_iban(raw)
    if not iban_is_valid(v):
        errors["iban"] = "Not a valid IBAN (checksum failed)"
        return None
    return v


def bic(raw: str | None, errors: FormErrors) -> str | None:
    if raw is None:
        return None
    v = raw.replace(" ", "").upper()
    if not _BIC.match(v):
        errors["bic"] = "Not a valid BIC/SWIFT code"
        return None
    return v
