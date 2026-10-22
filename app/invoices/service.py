"""Invoice lifecycle: DRAFT -> APPROVED -> SENT (-> PAID) or DRAFT -> CANCELLED.

Numbers are assigned only on approval, inside the approval transaction, so rejected drafts
and failed approvals never leave gaps in the sequence.
"""

import asyncio
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.audit import audit
from app.companies import default_bank_account
from app.config import Settings
from app.db.models import (
    Company,
    CompanyBankAccount,
    Counterparty,
    Invoice,
    InvoiceLine,
    InvoiceSource,
    InvoiceStatus,
    InvoiceTemplate,
)
from app.invoices.options import TemplateOptions
from app.invoices.pdf import html_to_pdf
from app.invoices.render import render_invoice_html
from app.invoices.view import BankDetails, InvoiceView, Line, Party, party_from_company
from app.money import CENT, to_money


class InvoiceError(Exception):
    """A user-facing reason why an invoice action cannot be done."""


@dataclass
class LineInput:
    description: str
    quantity: Decimal
    unit_price: Decimal  # net unit price unless include_vat
    unit: str | None = None


@dataclass
class InvoiceBundle:
    invoice: Invoice
    issuer: Company
    client: Counterparty


def check_complete(b: "InvoiceBundle") -> str:
    """Empty string when issuer and client have everything an invoice needs."""
    from app.invoices.completeness import labels, missing_client, missing_issuer

    parts = []
    issuer_gaps = missing_issuer(b.issuer, b.invoice.currency)
    if issuer_gaps:
        parts.append(f"{b.issuer.code}: {', '.join(issuer_gaps)} (admin panel)")
    client_gaps = missing_client(b.client)
    if client_gaps:
        parts.append(f"{b.client.name}: {labels(client_gaps)}")
    return "; ".join(parts)


def number_for(company: Company, n: int) -> str:
    return f"{company.invoice_prefix or ''}{n:010d}"


def _net_unit_price(price: Decimal, include_vat: bool, vat_rate: Decimal) -> Decimal:
    if not include_vat or vat_rate == 0:
        return to_money(price)
    return (price / (1 + vat_rate / 100)).quantize(CENT, rounding=ROUND_HALF_UP)


def _build_lines(lines: list[LineInput], include_vat: bool, vat_rate: Decimal) -> list[InvoiceLine]:
    out = []
    for i, li in enumerate(lines):
        unit_price = _net_unit_price(li.unit_price, include_vat, vat_rate)
        amount = (li.quantity * unit_price).quantize(CENT, rounding=ROUND_HALF_UP)
        out.append(
            InvoiceLine(
                position=i,
                description=li.description.strip(),
                quantity=li.quantity,
                unit=li.unit,
                unit_price=unit_price,
                amount=amount,
            )
        )
    return out


def _apply_totals(inv: Invoice) -> None:
    net = to_money(sum((li.amount for li in inv.lines), Decimal(0)))
    inv.amount_net = net
    inv.amount_vat = to_money(net * inv.vat_rate / 100)
    inv.amount_gross = inv.amount_net + inv.amount_vat


def needs_two_approvals(inv: Invoice, settings: Settings) -> bool:
    """Above MAX_INVOICE_AMOUNT an invoice needs two different approvers."""
    return abs(inv.amount_gross) > settings.max_invoice_amount


async def load_bundle(session: AsyncSession, invoice_id: int) -> InvoiceBundle:
    inv = await session.scalar(
        select(Invoice).options(selectinload(Invoice.lines)).where(Invoice.id == invoice_id)
    )
    if inv is None:
        raise InvoiceError("Invoice not found")
    issuer = await session.scalar(
        select(Company)
        .options(selectinload(Company.bank_accounts))
        .where(Company.id == inv.issuer_id)
    )
    client = await session.get(Counterparty, inv.counterparty_id)
    assert issuer is not None and client is not None
    return InvoiceBundle(inv, issuer, client)


async def create_draft(
    session: AsyncSession,
    settings: Settings,
    *,
    issuer: Company,
    client: Counterparty,
    lines: list[LineInput],
    currency: str | None = None,
    vat_rate: Decimal | None = None,
    include_vat: bool = False,
    due_in_days: int | None = None,
    notes: str | None = None,
    created_by: int | None = None,
    source: InvoiceSource = InvoiceSource.MANUAL,
) -> Invoice:
    if not lines:
        raise InvoiceError("An invoice needs at least one line with an amount")
    currency = currency or issuer.default_currency
    rate = Decimal(str(issuer.default_vat_rate if vat_rate is None else vat_rate))
    today = date.today()
    days = issuer.default_payment_terms_days if due_in_days is None else due_in_days
    bank = default_bank_account(issuer, currency)
    inv = Invoice(
        issuer_id=issuer.id,
        counterparty_id=client.id,
        template_id=issuer.default_template_id,
        bank_account_id=bank.id if bank else None,
        description="",
        notes=notes,
        vat_rate=rate,
        currency=currency,
        issue_date=today,
        due_date=today + timedelta(days=days),
        status=InvoiceStatus.DRAFT,
        source=source,
        created_by=created_by,
        lines=_build_lines(lines, include_vat, rate),
    )
    _apply_totals(inv)
    session.add(inv)
    await session.flush()
    audit(
        session,
        f"tg:{created_by}" if created_by else "system",
        "invoice.drafted",
        f"invoice:{inv.id}",
        {
            "issuer": issuer.code,
            "client": client.name,
            "gross": str(inv.amount_gross),
            "currency": currency,
        },
    )
    return inv


async def update_draft(
    session: AsyncSession,
    settings: Settings,
    bundle: InvoiceBundle,
    *,
    issuer: Company,
    client: Counterparty,
    lines: list[LineInput],
    currency: str | None,
    vat_rate: Decimal | None,
    include_vat: bool,
    due_in_days: int | None,
    notes: str | None,
    actor: str,
) -> Invoice:
    inv = bundle.invoice
    if inv.status != InvoiceStatus.DRAFT:
        raise InvoiceError("Only drafts can be edited")
    if not lines:
        raise InvoiceError("An invoice needs at least one line with an amount")
    inv.issuer_id, inv.counterparty_id = issuer.id, client.id
    inv.currency = currency or issuer.default_currency
    inv.vat_rate = Decimal(str(issuer.default_vat_rate if vat_rate is None else vat_rate))
    bank = default_bank_account(issuer, inv.currency)
    inv.bank_account_id = bank.id if bank else None
    inv.template_id = issuer.default_template_id
    if due_in_days is not None:
        inv.due_date = (inv.issue_date or date.today()) + timedelta(days=due_in_days)
    inv.notes = notes
    inv.lines.clear()
    await session.flush()
    inv.lines.extend(_build_lines(lines, include_vat, inv.vat_rate))
    _apply_totals(inv)
    inv.first_approved_by = inv.first_approved_at = None  # changed: approve afresh
    audit(
        session,
        actor,
        "invoice.edited",
        f"invoice:{inv.id}",
        {"gross": str(inv.amount_gross), "currency": inv.currency},
    )
    return inv


def draft_as_dict(b: InvoiceBundle) -> dict[str, Any]:
    """Current draft in the parser's shape, for edit requests."""
    inv = b.invoice
    days = (inv.due_date - inv.issue_date).days if inv.due_date and inv.issue_date else None
    return {
        "intent": "invoice",
        "issuer_code": b.issuer.code,
        "client_id": b.client.id,
        "client_text": b.client.name,
        "currency": inv.currency,
        "vat_rate": str(inv.vat_rate),
        "amounts_include_vat": False,
        "lines": [
            {
                "description": li.description,
                "quantity": str(li.quantity),
                "unit_price": str(li.unit_price),
                "unit": li.unit,
            }
            for li in inv.lines
        ],
        "due_in_days": days,
        "notes": inv.notes,
    }


# --- rendering ------------------------------------------------------------------------


def _party_from_client(c: Counterparty) -> Party:
    return Party(
        name=c.name,
        address=c.address or "",
        city=c.city or "",
        country=c.country or "",
        eik=c.eik or "",
        vat_id=c.vat_id or "",
        manager=c.contact_name or "",
        email=c.email or "",
        phone=c.phone or "",
    )


def snapshot_for(
    issuer: Company,
    client: Counterparty,
    bank: CompanyBankAccount | None,
    template: InvoiceTemplate | None,
) -> dict[str, Any]:
    return {
        "supplier": party_from_company(issuer).__dict__,
        "customer": _party_from_client(client).__dict__,
        "bank": BankDetails(bank.iban, bank.bic or "", bank.bank_name or "").__dict__
        if bank
        else None,
        "no_vat_note": issuer.no_vat_note or "",
        "logo_path": issuer.logo_path,
        "layout": template.layout if template else "classic",
        "options": dict(template.options) if template else {},
    }


async def _template(session: AsyncSession, inv: Invoice) -> InvoiceTemplate | None:
    return await session.get(InvoiceTemplate, inv.template_id) if inv.template_id else None


def _logo_uri(logo: str | None) -> str | None:
    if not logo or not Path(logo).is_file():
        return None
    return Path(logo).resolve().as_uri()


async def render_bundle_pdf(session: AsyncSession, b: InvoiceBundle) -> bytes:
    inv = b.invoice
    if inv.snapshot:
        snap = inv.snapshot
    else:
        bank = next((a for a in b.issuer.bank_accounts if a.id == inv.bank_account_id), None)
        snap = snapshot_for(b.issuer, b.client, bank, await _template(session, inv))
    logo_url = await asyncio.to_thread(_logo_uri, snap.get("logo_path"))
    view = InvoiceView(
        number=inv.number or "DRAFT",
        issue_date=inv.issue_date or date.today(),
        due_date=inv.due_date,
        supplier=Party(**snap["supplier"]),
        customer=Party(**snap["customer"]),
        lines=[Line(li.description, li.quantity, li.unit_price, li.unit or "") for li in inv.lines],
        currency=inv.currency,
        vat_rate=inv.vat_rate,
        bank=BankDetails(**snap["bank"]) if snap.get("bank") else None,
        notes=inv.notes or "",
        no_vat_note=snap.get("no_vat_note", ""),
        logo_url=logo_url,
        is_draft=inv.status == InvoiceStatus.DRAFT,
        credit_for=await credit_for_label(session, inv),
    )
    options = TemplateOptions.parse_lenient(snap.get("options"))
    if view.is_credit:
        options = options.model_copy(update={"title": "CREDIT NOTE"})
    html = render_invoice_html(view, snap.get("layout", "classic"), options)
    return await html_to_pdf(html)


def pdf_filename(inv: Invoice) -> str:
    kind = "credit-note" if inv.kind == CREDIT_NOTE else "invoice"
    return f"{kind}-{inv.number}.pdf" if inv.number else f"draft-{inv.id}.pdf"


def public_url(settings: Settings, inv: Invoice) -> str | None:
    if not inv.public_token or not settings.public_base_url:
        return None
    return f"{settings.public_base_url.rstrip('/')}/i/{inv.public_token}"


# --- credit notes -----------------------------------------------------------------------

CREDIT_NOTE = "credit_note"
CREDITABLE = (InvoiceStatus.APPROVED, InvoiceStatus.SENT, InvoiceStatus.PAID)


async def open_credit_note(session: AsyncSession, invoice_id: int) -> Invoice | None:
    return await session.scalar(
        select(Invoice).where(
            Invoice.kind == CREDIT_NOTE,
            Invoice.credited_invoice_id == invoice_id,
            Invoice.status != InvoiceStatus.CANCELLED,
        )
    )


async def create_credit_note(
    session: AsyncSession, original: InvoiceBundle, *, reason: str, created_by: int | None
) -> Invoice:
    """A DRAFT credit note for the full amount of an issued invoice. It is numbered in the
    issuer's normal series when approved (accountant to confirm the series and wording)."""
    orig = original.invoice
    if orig.kind == CREDIT_NOTE:
        raise InvoiceError("A credit note can't be credited")
    if orig.status not in CREDITABLE or not orig.number:
        raise InvoiceError("Only issued invoices can be credited; reject a draft instead")
    existing = await open_credit_note(session, orig.id)
    if existing:
        raise InvoiceError(
            f"Invoice {orig.number} already has credit note "
            + (existing.number or f"draft #{existing.id}")
        )
    note = Invoice(
        kind=CREDIT_NOTE,
        credited_invoice_id=orig.id,
        issuer_id=orig.issuer_id,
        counterparty_id=orig.counterparty_id,
        template_id=orig.template_id,
        bank_account_id=orig.bank_account_id,
        description=f"Credit note to invoice {orig.number}",
        notes=reason.strip() or None,
        vat_rate=orig.vat_rate,
        currency=orig.currency,
        issue_date=date.today(),
        due_date=None,
        status=InvoiceStatus.DRAFT,
        source=orig.source,
        created_by=created_by,
        lines=[
            InvoiceLine(
                position=li.position,
                description=li.description,
                quantity=li.quantity,
                unit=li.unit,
                unit_price=-li.unit_price,
                amount=-li.amount,
            )
            for li in orig.lines
        ],
    )
    _apply_totals(note)
    session.add(note)
    await session.flush()
    audit(
        session,
        f"tg:{created_by}" if created_by else "system",
        "credit_note.drafted",
        f"invoice:{note.id}",
        {"credits": orig.number, "gross": str(note.amount_gross), "reason": reason},
    )
    return note


async def _credit_original(session: AsyncSession, note: Invoice, actor_tg: int) -> None:
    orig = await session.get(Invoice, note.credited_invoice_id)
    if orig is None or orig.status not in CREDITABLE:
        raise InvoiceError("The credited invoice is no longer open")
    if orig.snapshot:  # the credit note names the same parties as the invoice it corrects
        note.snapshot = {**orig.snapshot, **{k: note.snapshot[k] for k in ("layout", "options")}}
    if orig.status != InvoiceStatus.PAID:  # a paid one stays PAID: the refund is manual
        orig.status = InvoiceStatus.CANCELLED
    audit(
        session,
        f"tg:{actor_tg}",
        "invoice.credited",
        f"invoice:{orig.id}",
        {"number": orig.number, "status": orig.status.value, "credit_note_id": note.id},
    )


async def credit_for_label(session: AsyncSession, inv: Invoice) -> str:
    if inv.kind != CREDIT_NOTE or not inv.credited_invoice_id:
        return ""
    orig = await session.get(Invoice, inv.credited_invoice_id)
    if orig is None:
        return "?"
    return f"{orig.number} of {orig.issue_date:%d.%m.%Y}" if orig.issue_date else orig.number or ""


# --- state transitions ----------------------------------------------------------------


_issuer_locks: dict[int, asyncio.Lock] = {}


async def approve(
    session: AsyncSession, settings: Settings, b: InvoiceBundle, approver_tg: int
) -> bytes | None:
    """Number, freeze and render a draft, then commit. Rolls back on any failure.

    Above MAX_INVOICE_AMOUNT the first approval is only recorded (returns None) and a second,
    different approver completes it.

    Approvals for one issuer run one at a time in-process (so a double click or two approvers
    can't race), and the DB-level atomic increment still guards across processes.
    """
    lock = _issuer_locks.setdefault(b.issuer.id, asyncio.Lock())
    async with lock:
        try:
            pdf = await _approve(session, settings, b, approver_tg)
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
        return pdf


async def _approve(
    session: AsyncSession, settings: Settings, b: InvoiceBundle, approver_tg: int
) -> bytes | None:
    inv = b.invoice
    await session.refresh(inv, ["status"])  # another click may have approved it meanwhile
    if inv.status != InvoiceStatus.DRAFT:
        raise InvoiceError(f"Invoice is already {inv.status.value.lower()}")
    incomplete = check_complete(b)
    if incomplete:
        raise InvoiceError(f"Cannot approve yet. Missing: {incomplete}")
    if needs_two_approvals(inv, settings):
        await session.refresh(inv, ["first_approved_by"])
        if inv.first_approved_by is None:
            inv.first_approved_by, inv.first_approved_at = approver_tg, datetime.now(UTC)
            audit(
                session,
                f"tg:{approver_tg}",
                "invoice.first_approval",
                f"invoice:{inv.id}",
                {"gross": str(inv.amount_gross), "currency": inv.currency},
            )
            return None
        if inv.first_approved_by == approver_tg:
            raise InvoiceError(
                "You already gave the first approval. A different approver must give the second."
            )
    # Atomic increment: concurrent approvals for the same issuer can never share a number.
    n = await session.scalar(
        update(Company)
        .where(Company.id == b.issuer.id)
        .values(invoice_next_number=Company.invoice_next_number + 1)
        .returning(Company.invoice_next_number)
    )
    assert n is not None
    inv.number = number_for(b.issuer, n - 1)
    today = date.today()
    if inv.issue_date and inv.due_date:
        inv.due_date = today + (inv.due_date - inv.issue_date)
    inv.issue_date = today
    bank = next((a for a in b.issuer.bank_accounts if a.id == inv.bank_account_id), None)
    inv.snapshot = snapshot_for(b.issuer, b.client, bank, await _template(session, inv))
    if inv.kind == CREDIT_NOTE:
        await _credit_original(session, inv, approver_tg)
    inv.status = InvoiceStatus.APPROVED
    inv.approved_by = approver_tg
    inv.approved_at = datetime.now(UTC)
    inv.public_token = secrets.token_urlsafe(24)
    pdf = await render_bundle_pdf(session, b)  # any failure here rolls the number back
    folder = settings.uploads_dir / "invoices" / b.issuer.code
    path = folder / f"{inv.number}.pdf"
    await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_bytes, pdf)
    inv.pdf_path = str(path)
    audit(
        session,
        f"tg:{approver_tg}",
        "invoice.approved",
        f"invoice:{inv.id}",
        {"number": inv.number, "gross": str(inv.amount_gross), "currency": inv.currency},
    )
    return pdf


async def reject(session: AsyncSession, b: InvoiceBundle, actor: str) -> None:
    inv = b.invoice
    if inv.status != InvoiceStatus.DRAFT:
        raise InvoiceError(f"Invoice is already {inv.status.value.lower()}")
    inv.status = InvoiceStatus.CANCELLED
    audit(session, actor, "invoice.rejected", f"invoice:{inv.id}")


async def stored_pdf(session: AsyncSession, b: InvoiceBundle) -> bytes:
    """The issued PDF from disk; re-rendered from the frozen snapshot if the file is gone."""
    inv = b.invoice
    if inv.pdf_path and await asyncio.to_thread(Path(inv.pdf_path).is_file):
        return await asyncio.to_thread(Path(inv.pdf_path).read_bytes)
    return await render_bundle_pdf(session, b)
