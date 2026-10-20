from decimal import Decimal

from app.db.models import Counterparty, Invoice, InvoiceStatus, MatchConfidence
from app.payments.matching import Candidate, match_transfer, number_in_reference


def cand(
    inv_id: int,
    number: str,
    gross: str,
    client: str = "Client Ltd",
    iban: str | None = None,
    currency: str = "EUR",
) -> Candidate:
    inv = Invoice(
        id=inv_id,
        number=number,
        amount_gross=Decimal(gross),
        currency=currency,
        status=InvoiceStatus.SENT,
    )
    return Candidate(inv, Counterparty(name=client, iban=iban))


A = cand(1, "0000000041", "1200.00", "Client Ltd", "BG80BNBG96611020345678")
B = cand(2, "0000000042", "500.00", "Other OOD")
C = cand(3, "0000000043", "500.00", "Third EAD")


def m(
    amount: str,
    ref: str | None = None,
    payer: str | None = None,
    acc: str | None = None,
    pool=None,
    currency: str = "EUR",
):  # noqa: ANN001, ANN201
    return match_transfer(Decimal(amount), currency, ref, payer, acc, pool or [A, B, C])


def test_reference_and_amount_is_high() -> None:
    r = m("1200.00", ref="Payment for invoice 0000000041")
    assert (r.confidence, r.invoice.id, r.method) == (MatchConfidence.HIGH, 1, "reference+amount")


def test_reference_without_leading_zeros() -> None:
    assert number_in_reference("0000000041", "INV 41 thanks")
    assert not number_in_reference("0000000041", "INV 410")
    assert number_in_reference("ZX0000000041", "zx-0000000041")
    r = m("500.00", ref="inv. no 42")
    assert (r.confidence, r.invoice.id) == (MatchConfidence.HIGH, 2)


def test_reference_amount_differs_is_medium() -> None:
    r = m("1000.00", ref="0000000041 partial")
    assert (r.confidence, r.invoice.id) == (MatchConfidence.MEDIUM, 1)


def test_amount_and_payer_is_high() -> None:
    assert m("500.00", payer="OTHER Ood.").invoice.id == 2
    r = m("1200.00", payer="Someone else", acc="BG80 BNBG 9661 1020 3456 78")
    assert (r.confidence, r.method) == (MatchConfidence.HIGH, "amount+payer")


def test_amount_only_is_medium() -> None:
    r = m("1200.00", payer="Unknown Payer")
    assert (r.confidence, r.invoice.id, r.method) == (MatchConfidence.MEDIUM, 1, "amount only")


def test_ambiguous_amount_is_low_with_candidates() -> None:
    r = m("500.00", payer="Nobody")
    assert r.confidence == MatchConfidence.LOW and r.invoice is None
    assert {c.invoice.id for c in r.candidates} == {2, 3}


def test_no_match_lists_nearest_and_respects_currency() -> None:
    r = m("777.00")
    assert r.confidence == MatchConfidence.LOW and r.method == "no match"
    assert r.candidates[0].invoice.id in (2, 3)
    usd = m("1200.00", ref="0000000041", currency="USD")
    assert usd.confidence == MatchConfidence.LOW and usd.candidates == []
