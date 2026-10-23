"""Tie an incoming bank transfer to one of our open invoices.

Order (CLAUDE.md, Flow B): invoice number in the payment reason -> exact amount + payer IBAN or
name -> exact amount among the issuer's open invoices -> ambiguous. Only HIGH is applied
automatically; MEDIUM/LOW go to a human in Telegram.

Amounts are compared with what is still due (`Invoice.remaining`), so instalments work: the
invoice number in the reason plus an amount up to the remainder is a (partial) payment.
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal

from app.db.models import Counterparty, Invoice, MatchConfidence

_LEGAL_SUFFIXES = re.compile(
    r"\b(ltd|limited|llc|inc|gmbh|ood|eood|ead|ad|plc|sa|srl|bv|ag|corp|co)\b\.?", re.I
)


@dataclass
class Candidate:
    invoice: Invoice
    client: Counterparty


@dataclass
class MatchResult:
    confidence: MatchConfidence
    invoice: Invoice | None
    method: str
    candidates: list[Candidate] = field(default_factory=list)


def _norm_text(s: str | None) -> str:
    return re.sub(r"[^0-9a-z]+", "", (s or "").casefold())


def norm_company_name(s: str | None) -> str:
    """Lower-case alphanumerics without legal suffixes ("Nove Solida Ltd." -> "novesolida")."""
    return _norm_name(s)


def _norm_name(s: str | None) -> str:
    return _norm_text(_LEGAL_SUFFIXES.sub(" ", s or ""))


def _norm_account(s: str | None) -> str:
    return re.sub(r"\s+", "", s or "").upper()


def number_in_reference(number: str, reference: str | None) -> bool:
    """Invoice number appears in the payment reason, ignoring spaces/dashes and, for all-digit
    numbers, also matching without leading zeros (people often type "INV 42" for 0000000042)."""
    if not reference or not number:
        return False
    ref = _norm_text(reference)
    if _norm_text(number) in ref:
        return True
    digits = number.lstrip("0")
    if number.isdigit() and len(digits) >= 2:
        return re.search(rf"(?<!\d)0*{re.escape(digits)}(?!\d)", reference) is not None
    return False


def payer_matches(client: Counterparty, payer_name: str | None, payer_account: str | None) -> bool:
    if client.iban and payer_account and _norm_account(client.iban) == _norm_account(payer_account):
        return True
    a, b = _norm_name(client.name), _norm_name(payer_name)
    return bool(a) and bool(b) and (a == b or (len(a) >= 5 and (a in b or b in a)))


def match_transfer(
    amount: Decimal,
    currency: str,
    reference: str | None,
    payer_name: str | None,
    payer_account: str | None,
    open_invoices: list[Candidate],
) -> MatchResult:
    pool = [c for c in open_invoices if c.invoice.currency == currency]

    by_number = [
        c for c in pool if c.invoice.number and number_in_reference(c.invoice.number, reference)
    ]
    if len(by_number) == 1:
        c = by_number[0]
        due = c.invoice.remaining
        if amount == due:
            return MatchResult(MatchConfidence.HIGH, c.invoice, "reference+amount", [c])
        if 0 < amount < due:
            return MatchResult(MatchConfidence.HIGH, c.invoice, "reference, partial", [c])
        return MatchResult(MatchConfidence.MEDIUM, c.invoice, "reference, more than due", [c])

    same_amount = [c for c in pool if c.invoice.remaining == amount]
    by_payer = [c for c in same_amount if payer_matches(c.client, payer_name, payer_account)]
    if len(by_payer) == 1:
        return MatchResult(MatchConfidence.HIGH, by_payer[0].invoice, "amount+payer", by_payer)
    if len(same_amount) == 1:
        return MatchResult(
            MatchConfidence.MEDIUM, same_amount[0].invoice, "amount only", same_amount
        )
    if len(by_number) > 1 or len(same_amount) > 1:
        cands = by_number or by_payer or same_amount
        return MatchResult(MatchConfidence.LOW, None, "ambiguous", cands[:5])

    nearest = sorted(pool, key=lambda c: abs(c.invoice.remaining - amount))[:5]
    return MatchResult(MatchConfidence.LOW, None, "no match", nearest)
