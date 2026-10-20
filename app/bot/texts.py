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
    "<b>Commands</b>\n"
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
