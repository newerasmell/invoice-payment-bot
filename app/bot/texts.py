"""All user-facing bot strings live here."""

from html import escape

from app.bank.base import Balance, Transfer
from app.db.models import Company, TransferDirection
from app.money import fmt_money
from app.redact import redact_iban

START = (
    "<b>Invoice &amp; Payment Bot</b>\n"
    "Hi {name}, you are signed in as <b>{role}</b>.\n"
    "Send /help to see what I can do."
)

HELP = (
    "<b>Invoices</b>\n"
    "Just write what to invoice, e.g. <i>Invoice Client Ltd 1200 EUR from Z, consulting</i>.\n"
    "I prepare a draft PDF for approval; nothing is sent without your click.\n\n"
    "<b>Commands</b>\n"
    "/invoices - recent invoices\n"
    "/companies - company profiles\n"
    "/balance &lt;company&gt; - account balance\n"
    "/help - this message"
)

HELP_DEV = (
    "\n\n<b>Dev (mock bank only)</b>\n"
    "/dev_simulate_incoming &lt;company&gt; &lt;amount&gt; &lt;payer&gt; [reference]\n"
    "/dev_simulate_outgoing &lt;company&gt; &lt;amount&gt; &lt;beneficiary&gt; [iban] [reference]\n"
    'Use quotes for names with spaces: "Client Ltd".'
)

NO_COMPANIES = "No companies yet. Add them to config/companies.yaml and restart."
COMPANY_NOT_FOUND = "Unknown company <code>{key}</code>. Known: {known}"
NO_BANK_ACCOUNT = "Company <b>{name}</b> has no bank account linked to the bank API."
BALANCE_USAGE = "Usage: /balance &lt;company&gt;"
BANK_ERROR = "Could not reach the bank: {error}"
DEV_USAGE_IN = (
    "Usage: /dev_simulate_incoming &lt;company&gt; &lt;amount&gt; &lt;payer&gt; [reference]"
)
DEV_USAGE_OUT = (
    "Usage: /dev_simulate_outgoing &lt;company&gt; &lt;amount&gt; &lt;beneficiary&gt; "
    "[iban] [reference]"
)
BAD_AMOUNT = "Invalid amount: <code>{value}</code>"
BAD_ARGS = "Could not parse the arguments: {error}"


def companies(items: list[Company]) -> str:
    lines = ["<b>Companies</b>"]
    for c in items:
        ids = " · ".join(x for x in (c.eik and f"EIK {c.eik}", c.vat_id and f"VAT {c.vat_id}") if x)
        lines.append(f"\n<b>{escape(c.code)}</b> - {escape(c.legal_name or c.name)}")
        if ids:
            lines.append(escape(ids))
        for a in c.bank_accounts:
            star = " (default)" if a.is_default else ""
            iban = escape(redact_iban(a.iban))
            lines.append(f"  {escape(a.currency)}: <code>{iban}</code>{star}")
    return "\n".join(lines)


def balance(company: Company, bal: Balance) -> str:
    return (
        f"<b>{escape(company.name)}</b> - account <code>{escape(bal.account)}</code>\n"
        f"Balance: <b>{fmt_money(bal.balance, bal.currency)}</b>\n"
        f"Available: {fmt_money(bal.available, bal.currency)}"
    )


def simulated(t: Transfer) -> str:
    arrow = "Incoming" if t.direction == TransferDirection.INCOMING else "Outgoing"
    return (
        f"<b>[MOCK] {arrow} transfer created</b>\n"
        f"ID: <code>{escape(t.external_id)}</code>\n"
        f"Amount: <b>{fmt_money(t.amount, t.currency)}</b>\n"
        f"Payer: {escape(t.payer_name or '-')}\n"
        f"Beneficiary: {escape(t.beneficiary_name or '-')}\n"
        f"Reference: {escape(t.reference or '-')}\n"
        f"Status: {escape(t.status)}"
    )


# --- invoices -------------------------------------------------------------------------

PARSER_DISABLED = (
    "Free-text invoices are off: ANTHROPIC_API_KEY is not set. Ask an admin to add it."
)
READING = "Reading your request…"
NOT_AN_INVOICE = (
    "I can prepare invoices from a message like:\n"
    "<i>Invoice Client Ltd 1200 EUR from Z for consulting services</i>\n\nSend /help for commands."
)
NEED_AMOUNT = "I need at least one item with an amount to draft an invoice."
NO_COMPANIES_FOR_USER = "No companies are available to you. Create one in the admin panel first."
PICK_ISSUER = "Which of our companies issues this invoice?"
PICK_CLIENT = "Who is the client{hint}?"
CREATE_CLIENT = "➕ New client “{name}”"
CANCEL = "✖ Cancel"
CANCELLED = "Cancelled."
FLOW_EXPIRED = "This request has expired. Please send it again."
EDIT_PROMPT = (
    "What should change? E.g. <i>make it 1500 EUR</i>, <i>add a line Hosting 2 × 50</i>, "
    "<i>VAT 0%</i>, <i>due in 30 days</i>. Send /cancel to stop editing."
)
EDIT_FAILED_CLIENT = "I couldn't match that client; the draft keeps {name}."
ONLY_APPROVERS = "Only approvers can approve invoices."
NOT_YOUR_COMPANY = "You are not allowed to act for this company."
PDF_UNAVAILABLE = "(PDF engine unavailable on the server: {error})"
NOT_NOW = "OK. Use /invoices to send it later."
NO_INVOICES = "No invoices yet."

BTN_APPROVE, BTN_EDIT, BTN_REJECT = "✅ Approve", "✏️ Edit", "❌ Reject"
BTN_SEND, BTN_NOT_NOW = "📤 Send to client", "Not now"


def draft_caption(inv, issuer, client, warnings: list[str]) -> str:  # noqa: ANN001
    lines = [
        f"<b>Draft invoice</b> · {escape(issuer.code)} → {escape(client.name)}",
    ]
    for li in inv.lines[:8]:
        qty = f"{li.quantity.normalize():f}"
        lines.append(
            f"• {escape(li.description[:60])} — {qty} × {li.unit_price:,.2f} = {li.amount:,.2f}"
        )
    if len(inv.lines) > 8:
        lines.append(f"… and {len(inv.lines) - 8} more")
    lines.append(
        f"Net {inv.amount_net:,.2f} · VAT {inv.vat_rate.normalize():f}% {inv.amount_vat:,.2f}"
    )
    lines.append(f"<b>Total {fmt_money(inv.amount_gross, inv.currency)}</b>")
    if inv.due_date:
        lines.append(f"Due {inv.due_date:%d.%m.%Y} (counted from approval)")
    lines += [f"⚠️ {escape(w)}" for w in warnings]
    return "\n".join(lines)[:1000]


def approved_caption(inv, issuer, client, link: str | None) -> str:  # noqa: ANN001
    text = (
        f"✅ <b>Invoice {escape(inv.number)}</b> approved\n"
        f"{escape(issuer.legal_name or issuer.name)} → {escape(client.name)}\n"
        f"<b>{fmt_money(inv.amount_gross, inv.currency)}</b>"
    )
    if inv.due_date:
        text += f", due {inv.due_date:%d.%m.%Y}"
    if link:
        text += f"\nLink for the client: {escape(link)}"
    return text


def delivery_report(number: str, results, dry_run: bool) -> str:  # noqa: ANN001
    icons = {"sent": "✅", "dry_run": "🧪", "failed": "❗", "skipped": "➖"}
    head = f"<b>Invoice {escape(number)}</b>" + (
        " — DRY RUN, nothing reached the client" if dry_run else ""
    )
    rows = [
        f"{icons.get(r.status, '•')} {escape(r.channel)} → {escape(r.recipient)}: "
        + escape(r.status)
        + (f" ({escape(r.detail)})" if r.detail else "")
        for r in results
    ]
    return "\n".join([head, *rows])


def invoice_list(items) -> str:  # noqa: ANN001
    rows = ["<b>Recent invoices</b>"]
    for inv, issuer_code, client_name in items:
        num = inv.number or f"draft #{inv.id}"
        rows.append(
            f"{escape(num)} · {escape(issuer_code)} → {escape(client_name)} · "
            f"{fmt_money(inv.amount_gross, inv.currency)} · {inv.status.value}"
        )
    return "\n".join(rows)
