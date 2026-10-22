"""Texts sent to clients together with an invoice. Plain English, no markup."""

from app.db.models import Company, Counterparty, Invoice
from app.money import fmt_money


def _label(inv: Invoice) -> str:
    return "Credit note" if inv.kind == "credit_note" else "Invoice"


def subject(inv: Invoice, issuer: Company) -> str:
    return f"{_label(inv)} {inv.number} from {issuer.legal_name or issuer.name}"


def body(inv: Invoice, issuer: Company, client: Counterparty, link: str | None) -> str:
    snap = inv.snapshot or {}
    bank = snap.get("bank") or {}
    greeting = f"Dear {client.contact_name}," if client.contact_name else "Hello,"
    lines = [
        greeting,
        "",
        f"Please find attached {_label(inv).lower()} {inv.number} from "
        f"{issuer.legal_name or issuer.name} "
        f"for {fmt_money(inv.amount_gross, inv.currency)}"
        + (f", due on {inv.due_date:%d.%m.%Y}." if inv.due_date else "."),
    ]
    if bank.get("iban") and inv.kind != "credit_note":
        lines += ["", "Payment details:", f"IBAN: {bank['iban']}"]
        if bank.get("bic"):
            lines.append(f"BIC: {bank['bic']}")
        lines.append(f"Reference: {inv.number}")
    if link:
        lines += ["", f"View or download the {_label(inv).lower()}: {link}"]
    lines += ["", "Kind regards,", issuer.legal_name or issuer.name]
    return "\n".join(lines)


def telegram_caption(inv: Invoice, issuer: Company, link: str | None) -> str:
    text = (
        f"{_label(inv)} {inv.number} from {issuer.legal_name or issuer.name}\n"
        f"Amount: {fmt_money(inv.amount_gross, inv.currency)}"
    )
    if inv.due_date:
        text += f"\nDue: {inv.due_date:%d.%m.%Y}"
    if link:
        text += f"\n{link}"
    return text
