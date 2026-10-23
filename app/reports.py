"""Read-only figures for Admin → Finance and the Telegram /report: money in and out for a
period, what each transfer was tied to, receivables with what is still due and how late, and
the payments we still have to make. Nothing here changes data."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BankTransfer,
    Company,
    Counterparty,
    InstructionStatus,
    Invoice,
    InvoiceStatus,
    PaymentInstruction,
    PaymentMatch,
    TransferDirection,
)

ZERO = Decimal("0.00")
PRESETS = {
    "this_month": "This month",
    "last_month": "Last month",
    "last_30": "Last 30 days",
    "this_year": "This year",
}
BUCKETS = ("Not due", "1–30 days late", "31–60 days late", "60+ days late")


@dataclass
class Period:
    start: date
    end: date  # inclusive

    @property
    def label(self) -> str:
        return f"{self.start:%d.%m.%Y} – {self.end:%d.%m.%Y}"


def period_for(preset: str | None, start: str | None, end: str | None, today: date) -> Period:
    if start or end:
        try:
            s = date.fromisoformat(start) if start else today.replace(day=1)
            e = date.fromisoformat(end) if end else today
            return Period(min(s, e), max(s, e))
        except ValueError:
            pass
    first = today.replace(day=1)
    if preset == "last_month":
        last_end = first - timedelta(days=1)
        return Period(last_end.replace(day=1), last_end)
    if preset == "last_30":
        return Period(today - timedelta(days=29), today)
    if preset == "this_year":
        return Period(today.replace(month=1, day=1), today)
    return Period(first, today)


@dataclass
class TransferRow:
    bt: BankTransfer
    company: str
    when: date
    counterparty: str
    link: str  # invoice | instruction | dismissed | review | unmatched | pending
    label: str = ""
    invoice_id: int | None = None

    @property
    def signed(self) -> Decimal:
        return (
            -self.bt.amount if self.bt.direction == TransferDirection.OUTGOING else self.bt.amount
        )


@dataclass
class Totals:
    incoming: Decimal = ZERO
    outgoing: Decimal = ZERO
    count_in: int = 0
    count_out: int = 0

    @property
    def net(self) -> Decimal:
        return self.incoming - self.outgoing


def _day(dt: datetime | None) -> date | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC).date()


async def transfers(
    session: AsyncSession,
    period: Period,
    company_id: int | None = None,
    currency: str | None = None,
) -> tuple[list[TransferRow], dict[str, Totals]]:
    q = (
        select(BankTransfer, Company.code, PaymentMatch, Invoice.number)
        .outerjoin(Company, Company.id == BankTransfer.company_id)
        .outerjoin(PaymentMatch, PaymentMatch.transfer_id == BankTransfer.id)
        .outerjoin(Invoice, Invoice.id == PaymentMatch.invoice_id)
        .order_by(BankTransfer.id.desc())
        .limit(20000)
    )
    if company_id:
        q = q.where(BankTransfer.company_id == company_id)
    if currency:
        q = q.where(BankTransfer.currency == currency)
    executed = dict(
        (
            await session.execute(
                select(PaymentInstruction.matched_transfer_id, PaymentInstruction.id).where(
                    PaymentInstruction.matched_transfer_id.is_not(None)
                )
            )
        ).all()
    )
    rows: list[TransferRow] = []
    totals: dict[str, Totals] = defaultdict(Totals)
    for bt, code, match, number in (await session.execute(q)).all():
        when = _day(bt.booked_at) or _day(bt.created_at)
        if when is None or not (period.start <= when <= period.end):
            continue
        incoming = bt.direction == TransferDirection.INCOMING
        if bt.status != "processed":
            link, label = "pending", bt.status
        elif incoming and match and match.invoice_id:
            link, label = "invoice", number or ""
        elif incoming and match and match.method == "dismissed":
            link, label = "dismissed", "not an invoice payment"
        elif incoming and match:
            link, label = "review", "waiting for a decision"
        elif not incoming and bt.id in executed:
            link, label = "instruction", f"#{executed[bt.id]}"
        else:
            link, label = "unmatched", ""
        rows.append(
            TransferRow(
                bt,
                code or "?",
                when,
                (bt.payer_name if incoming else bt.beneficiary_name) or "",
                link,
                label,
                match.invoice_id if match else None,
            )
        )
        if bt.status == "processed":
            t = totals[bt.currency]
            if incoming:
                t.incoming += bt.amount
                t.count_in += 1
            elif bt.direction == TransferDirection.OUTGOING:
                t.outgoing += bt.amount
                t.count_out += 1
    rows.sort(key=lambda r: (r.when, r.bt.id), reverse=True)
    return rows, dict(totals)


@dataclass
class Receivables:
    count: int = 0
    issued: Decimal = ZERO
    paid: Decimal = ZERO
    credited: Decimal = ZERO

    @property
    def outstanding(self) -> Decimal:
        return self.issued + self.credited - self.paid


@dataclass
class OpenInvoice:
    invoice: Invoice
    company: str
    client: str
    days_late: int
    bucket: str


@dataclass
class OpenSummary:
    rows: list[OpenInvoice] = field(default_factory=list)
    by_bucket: dict[str, dict[str, Decimal]] = field(default_factory=dict)  # currency -> bucket
    partial: int = 0
    overpaid: list[OpenInvoice] = field(default_factory=list)


def _bucket(days_late: int) -> str:
    if days_late <= 0:
        return BUCKETS[0]
    if days_late <= 30:
        return BUCKETS[1]
    if days_late <= 60:
        return BUCKETS[2]
    return BUCKETS[3]


async def _invoices(session: AsyncSession, company_id: int | None, currency: str | None):  # noqa: ANN202
    q = (
        select(Invoice, Company.code, Counterparty.name)
        .join(Company, Company.id == Invoice.issuer_id)
        .join(Counterparty, Counterparty.id == Invoice.counterparty_id)
        .where(Invoice.status.not_in((InvoiceStatus.DRAFT, InvoiceStatus.CANCELLED)))
    )
    if company_id:
        q = q.where(Invoice.issuer_id == company_id)
    if currency:
        q = q.where(Invoice.currency == currency)
    return (await session.execute(q.order_by(Invoice.due_date, Invoice.id))).all()


async def receivables(
    session: AsyncSession,
    period: Period,
    company_id: int | None = None,
    currency: str | None = None,
) -> dict[str, Receivables]:
    """Invoices issued in the period (by issue date), what was paid and is still due."""
    out: dict[str, Receivables] = defaultdict(Receivables)
    for inv, _, _ in await _invoices(session, company_id, currency):
        if inv.issue_date is None or not (period.start <= inv.issue_date <= period.end):
            continue
        r = out[inv.currency]
        if inv.kind == "credit_note":
            r.credited += inv.amount_gross
            continue
        r.count += 1
        r.issued += inv.amount_gross
        r.paid += inv.paid
    return dict(out)


async def open_invoices(
    session: AsyncSession,
    today: date,
    company_id: int | None = None,
    currency: str | None = None,
) -> OpenSummary:
    """All invoices still owed to us today (any issue date), oldest due first."""
    out = OpenSummary()
    for inv, code, client in await _invoices(session, company_id, currency):
        if inv.kind == "credit_note":
            continue
        item = OpenInvoice(inv, code, client, 0, BUCKETS[0])
        if inv.remaining < 0:
            out.overpaid.append(item)
            continue
        if inv.status == InvoiceStatus.PAID or inv.remaining == 0:
            continue
        item.days_late = (today - inv.due_date).days if inv.due_date else 0
        item.bucket = _bucket(item.days_late)
        out.rows.append(item)
        cur = out.by_bucket.setdefault(inv.currency, dict.fromkeys(BUCKETS, ZERO))
        cur[item.bucket] += inv.remaining
        out.partial += inv.partially_paid
    out.rows.sort(key=lambda r: (-r.days_late, r.invoice.id))
    return out


async def payables(
    session: AsyncSession, company_id: int | None = None, currency: str | None = None
) -> list[tuple[PaymentInstruction, str]]:
    """Payments we still have to make (open payment instructions), soonest due first."""
    q = (
        select(PaymentInstruction, Company.code)
        .join(Company, Company.id == PaymentInstruction.from_company_id)
        .where(
            PaymentInstruction.status.in_(
                (
                    InstructionStatus.PROPOSED,
                    InstructionStatus.APPROVED,
                    InstructionStatus.AWAITING_BANK,
                )
            )
        )
        .order_by(PaymentInstruction.due_date.is_(None), PaymentInstruction.due_date)
    )
    if company_id:
        q = q.where(PaymentInstruction.from_company_id == company_id)
    if currency:
        q = q.where(PaymentInstruction.currency == currency)
    return list((await session.execute(q)).all())
