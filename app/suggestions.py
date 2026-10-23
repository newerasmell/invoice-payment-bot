"""Suggestions for Admin → Finance and /report (read-only; a human applies them):

- an incoming payment not tied to an invoice, with the most likely invoice and why;
- two or three such payments from the same payer that add up to what an invoice still owes;
- invoices left open by a few cents (bank fees), which can be closed;
- payments we have to make soon or are already late.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from itertools import combinations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BankTransfer,
    Counterparty,
    Invoice,
    PaymentMatch,
    TransferDirection,
)
from app.payments.matching import number_in_reference, payer_matches
from app.payments.sync import open_candidates
from app.reports import payables

SMALL_REMAINDER = Decimal("1.00")  # or 0.5% of the total, whichever is larger
CLOSE_AMOUNT = Decimal("0.02")  # within 2%: likely the invoice minus bank charges


@dataclass
class LinkSuggestion:
    transfer: BankTransfer
    invoice: Invoice
    client: Counterparty
    reasons: list[str]
    score: int


@dataclass
class SplitSuggestion:
    transfers: list[BankTransfer]
    invoice: Invoice
    client: Counterparty


@dataclass
class Suggestions:
    links: list[LinkSuggestion] = field(default_factory=list)
    unmatched_without_idea: int = 0
    splits: list[SplitSuggestion] = field(default_factory=list)
    small_remainders: list[tuple[Invoice, Counterparty]] = field(default_factory=list)
    payables_due: list = field(default_factory=list)

    @property
    def count(self) -> int:
        return (
            len(self.links) + len(self.splits) + len(self.small_remainders) + len(self.payables_due)
        )


async def unlinked_incoming(
    session: AsyncSession, company_id: int | None = None
) -> list[BankTransfer]:
    """Processed incoming payments not tied to an invoice and not dismissed."""
    q = (
        select(BankTransfer, PaymentMatch)
        .outerjoin(PaymentMatch, PaymentMatch.transfer_id == BankTransfer.id)
        .where(
            BankTransfer.direction == TransferDirection.INCOMING,
            BankTransfer.status == "processed",
            BankTransfer.company_id.is_not(None),
        )
        .order_by(BankTransfer.id.desc())
        .limit(500)
    )
    if company_id:
        q = q.where(BankTransfer.company_id == company_id)
    out = []
    for bt, match in (await session.execute(q)).all():
        if match is None or (match.invoice_id is None and match.method != "dismissed"):
            out.append(bt)
    return out


def _score(bt: BankTransfer, inv: Invoice, client: Counterparty) -> tuple[int, list[str]]:
    score, why = 0, []
    due = inv.remaining
    if inv.number and number_in_reference(inv.number, bt.reference):
        score, why = score + 3, [*why, "invoice number in the reason"]
    if bt.amount == due:
        score, why = score + 2, [*why, "same amount as still due"]
    elif due > 0 and bt.amount < due and abs(due - bt.amount) <= due * CLOSE_AMOUNT:
        score, why = score + 1, [*why, f"{due - bt.amount:,.2f} less than due (bank charges?)"]
    if payer_matches(client, bt.payer_name, bt.payer_account):
        score, why = score + 2, [*why, "payer is the client"]
    return score, why


async def build(session: AsyncSession, today: date, company_id: int | None = None) -> Suggestions:
    out = Suggestions()
    transfers = await unlinked_incoming(session, company_id)
    pools: dict[int, list] = {}
    used: set[int] = set()
    for bt in transfers:
        pool = pools.get(bt.company_id)
        if pool is None:
            pool = pools[bt.company_id] = await open_candidates(session, bt.company_id)
        best = None
        for c in pool:
            if c.invoice.currency != bt.currency:
                continue
            score, why = _score(bt, c.invoice, c.client)
            # A suggestion needs two independent signals, or the invoice number itself.
            if score >= 3 and (best is None or score > best.score):
                best = LinkSuggestion(bt, c.invoice, c.client, why, score)
        if best:
            out.links.append(best)
            used.add(bt.id)

    # Splits: 2-3 unlinked payments from one payer adding up to an invoice's remainder.
    by_payer: dict[tuple, list[BankTransfer]] = {}
    for bt in transfers:
        if bt.id not in used:
            key = (bt.company_id, bt.currency, (bt.payer_name or "").casefold())
            by_payer.setdefault(key, []).append(bt)
    for (company, currency, _), group in by_payer.items():
        if len(group) < 2:
            continue
        for c in pools.get(company) or await open_candidates(session, company):
            if c.invoice.currency != currency or c.invoice.remaining <= 0:
                continue
            if not payer_matches(c.client, group[0].payer_name, group[0].payer_account):
                continue
            hit = next(
                (
                    combo
                    for n in (2, 3)
                    for combo in combinations(group[:12], n)
                    if sum(t.amount for t in combo) == c.invoice.remaining
                ),
                None,
            )
            if hit:
                out.splits.append(SplitSuggestion(list(hit), c.invoice, c.client))
                used.update(t.id for t in hit)
                break
    out.unmatched_without_idea = len(transfers) - len(used)

    # Small remainders left open after a payment (typically bank charges).
    q = (
        select(Invoice, Counterparty)
        .join(Counterparty, Counterparty.id == Invoice.counterparty_id)
        .where(Invoice.amount_paid.is_not(None), Invoice.kind == "invoice")
    )
    if company_id:
        q = q.where(Invoice.issuer_id == company_id)
    for inv, client in (await session.execute(q)).all():
        rem = inv.remaining
        limit = max(
            SMALL_REMAINDER, (inv.amount_gross * Decimal("0.005")).quantize(Decimal("0.01"))
        )
        if inv.status.value in ("APPROVED", "SENT") and 0 < rem <= limit:
            out.small_remainders.append((inv, client))

    soon = today + timedelta(days=7)
    out.payables_due = [
        (i, code)
        for i, code in await payables(session, company_id)
        if i.due_date is not None and i.due_date <= soon
    ]
    out.links.sort(key=lambda s: (-s.score, -s.transfer.id))
    return out
