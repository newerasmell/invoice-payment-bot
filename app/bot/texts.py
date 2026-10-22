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
    "I prepare a draft PDF for approval; nothing is sent without your click.\n"
    "Send a photo or PDF (another invoice, a supplier's invoice to pay, a company card) and "
    "choose what to do with it.\n\n"
    "<b>Commands</b>\n"
    "/invoices - recent invoices\n"
    "/payments - recent bank transfers\n"
    "/instructions - payments we have to make\n"
    "/rules - automatic invoicing rules\n"
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


# --- payments -------------------------------------------------------------------------

NO_TRANSFERS = "No bank transfers seen yet."
NOT_A_PAYMENT = "Marked as not an invoice payment."
ONLY_APPROVERS_PAY = "Only approvers can confirm payments."
SYNC_RUNNING = "Checking the bank…"
SYNC_DONE = "Bank checked: {n} new payment event(s)."


def _payer(bt) -> str:  # noqa: ANN001
    who = escape(bt.payer_name or "unknown payer")
    if bt.payer_account:
        who += f" (<code>{escape(redact_iban(bt.payer_account))}</code>)"
    return who


def payment_paid(bt, company, inv) -> str:  # noqa: ANN001
    return (
        f"💰 <b>Received {fmt_money(bt.amount, bt.currency)}</b> from {_payer(bt)}\n"
        f"for invoice <b>{escape(inv.number)}</b>"
        + (f" ({escape(company.code)})" if company else "")
        + " — marked <b>PAID</b>."
        + (f"\nReason: {escape(bt.reference)}" if bt.reference else "")
    )


def payment_review(bt, company, method: str, suggested) -> str:  # noqa: ANN001
    head = f"❓ <b>Incoming {fmt_money(bt.amount, bt.currency)}</b> from {_payer(bt)}" + (
        f" to {escape(company.code)}" if company else ""
    )
    lines = [head]
    if bt.reference:
        lines.append(f"Reason: {escape(bt.reference)}")
    if suggested is not None:
        lines.append(
            f"Possible match: invoice <b>{escape(suggested.number)}</b> "
            f"({fmt_money(suggested.amount_gross, suggested.currency)}) — {escape(method)}."
        )
    else:
        lines.append(f"No certain match ({escape(method)}).")
    lines.append("Which invoice does it pay?")
    return "\n".join(lines)


def payment_confirmed(number: str, user_name: str) -> str:
    return f"✅ Confirmed as payment of invoice <b>{escape(number)}</b> by {escape(user_name)}."


def transfers_list(rows) -> str:  # noqa: ANN001
    out = ["<b>Recent bank transfers</b>"]
    for bt, code, match_number, needs_review in rows:
        arrow = {"incoming": "⬅️", "outgoing": "➡️"}.get(bt.direction.value, "🔁")
        other = bt.payer_name if bt.direction.value == "incoming" else bt.beneficiary_name
        when = f"{bt.booked_at:%d.%m %H:%M}" if bt.booked_at else ""
        if match_number:
            state = f"✅ {escape(match_number)}"
        elif needs_review:
            state = "❓ needs review"
        else:
            state = escape(bt.status)
        out.append(
            f"{arrow} {when} {escape(code or '?')} · {fmt_money(bt.amount, bt.currency)} · "
            f"{escape((other or '-')[:30])} · {state}"
        )
    return "\n".join(out)


# --- rules ----------------------------------------------------------------------------

NO_RULES = "No rules yet. Create them in the admin panel (Rules)."


def rule_draft_header(trigger: str, target: str, received, percent, amount, currency: str) -> str:  # noqa: ANN001
    return (
        f"🔁 <b>Rule</b>: {escape(trigger)} received {fmt_money(received, currency)} → "
        f"{escape(target)} invoices {escape(trigger)} {fmt_money(amount, currency)} "
        f"(−{percent.normalize():f}%)"
    )


def rules_list(rows) -> str:  # noqa: ANN001
    out = ["<b>Rules</b>"]
    for rule, trigger, target, description in rows:
        state = "✅" if rule.enabled else "⏸"
        pct = (rule.params or {}).get("percent", "1.5")
        out.append(
            f"{state} When <b>{escape(trigger)}</b> receives a payment → <b>{escape(target)}</b> "
            f"invoices {escape(trigger)} for the amount −{escape(str(pct))}%"
            + (f" ({escape(description[:40])})" if description else "")
        )
    return "\n".join(out)


# --- completeness & documents ---------------------------------------------------------

UNSUPPORTED_FILE = (
    "I can read photos, PNG/JPG/WebP images and PDF files. That file type isn't supported."
)
FILE_TOO_BIG = "That file is too big (max {mb} MB)."
COLLECT_NOTHING_FOUND = "I couldn't find that in your reply. {question}"


def issuer_incomplete(code: str, gaps: list[str], link: str | None) -> str:
    text = (
        f"⛔ <b>{escape(code)}</b> can't issue invoices yet. Its profile is missing: "
        f"<b>{escape(', '.join(gaps))}</b>.\nComplete it in the admin panel"
    )
    return text + (f": {escape(link)}" if link else ".")


def ask_client_details(name: str, missing: str, known: dict[str, str]) -> str:
    lines = [f"📋 To invoice <b>{escape(name)}</b> I still need: <b>{escape(missing)}</b>."]
    if known:
        lines.append("Known so far: " + escape("; ".join(f"{k}: {v}" for k, v in known.items())))
    lines.append(
        "Reply in text (e.g. <i>company no 12345678, 1 Main St, London, United Kingdom</i>) "
        "or send a photo/PDF with their details. /cancel to stop."
    )
    return "\n".join(lines)


def details_saved(name: str, fields: str) -> str:
    return f"✅ Saved for {escape(name)}: {escape(fields)}."


# --- documents ------------------------------------------------------------------------

DOC_MENU = "What should I do with this document?"
BTN_DOC_INVOICE = "📄 Issue a similar invoice"
BTN_DOC_CLIENT = "👤 Save as client"
BTN_DOC_COMPANY = "🏢 Create/update our company"
BTN_DOC_PAY = "💳 Invoice to pay"
READING_DOC = "Reading the document…"
NOT_A_DOCUMENT = "I couldn't find company or invoice details in that file."
PICK_PARTY = "Which company in the document do you mean?"
NO_PARTY = "I couldn't find that company's details in the document."
PICK_PAYER = "Which of our companies pays this invoice?"
BTN_CONFIRM = "✅ Confirm"


def party_line(p) -> str:  # noqa: ANN001
    bits = [p.name, p.company_number and f"no {p.company_number}", p.city, p.country]
    return escape(" · ".join(b for b in bits if b))


def client_saved(c, created: bool, changed: list[str], missing: str, link: str | None) -> str:  # noqa: ANN001
    head = "👤 New client saved" if created else "👤 Client updated"
    lines = [f"{head}: <b>{escape(c.name)}</b>"]
    details = [
        c.eik and f"Company no: {c.eik}",
        c.vat_id and f"VAT: {c.vat_id}",
        ", ".join(x for x in (c.address, c.city, c.country) if x),
        c.email and f"Email: {c.email}",
    ]
    lines += [escape(d) for d in details if d]
    if not created:
        lines.append(f"Filled: {escape(', '.join(changed))}" if changed else "Nothing new to add.")
    if missing:
        lines.append(f"⚠️ Still missing for invoicing: {escape(missing)}")
    if link:
        lines.append(f"Edit: {escape(link)}")
    return "\n".join(lines)


def company_proposal(code: str, fields: dict[str, str], bank: str | None, update: bool) -> str:
    head = (
        f"🏢 Update our company <b>{escape(code)}</b> with:"
        if update
        else f"🏢 Create our company <b>{escape(code)}</b>:"
    )
    lines = [head] + [f"• {escape(k)}: {escape(v)}" for k, v in fields.items()]
    if bank:
        lines.append(f"• bank account: {escape(bank)}")
    if update and not fields and not bank:
        lines.append("Nothing new to add.")
    lines.append("Confirm to save. You can change anything later in the admin panel.")
    return "\n".join(lines)


def company_saved(code: str, created: bool, link: str | None) -> str:
    text = f"✅ Company <b>{escape(code)}</b> {'created' if created else 'updated'}."
    return text + (f"\nCheck it, add the logo and templates: {escape(link)}" if link else "")


# --- payment instructions -----------------------------------------------------------------

BTN_APPROVE_PAYMENT, BTN_CANCEL_PAYMENT = "✅ Approve payment", "✖ Cancel"
ONLY_APPROVERS_PAYMENT = "Only approvers can approve payments."
NO_INSTRUCTIONS = "No open payment instructions."
INSTRUCTION_CANCELLED = "Payment instruction cancelled."


def instruction_card(i, payer) -> str:  # noqa: ANN001
    status = {
        "PROPOSED": "waiting for internal approval",
        "AWAITING_BANK": "approved, waiting for the bank",
        "EXECUTED": "executed ✅",
        "CANCELLED": "cancelled",
    }.get(i.status.value, i.status.value)
    lines = [
        f"💳 <b>Payment instruction #{i.id}</b> — {escape(status)}",
        f"From: <b>{escape(payer.code)}</b> ({escape(payer.legal_name or payer.name)})",
        f"To: <b>{escape(i.to_name)}</b>",
        f"IBAN: <code>{escape(i.to_iban)}</code>",
    ]
    if i.to_bic:
        lines.append(f"BIC: <code>{escape(i.to_bic)}</code>")
    if i.to_bank_name:
        lines.append(f"Bank: {escape(i.to_bank_name)}")
    lines.append(f"Amount: <code>{i.amount:.2f}</code> {escape(i.currency)}")
    if i.reference:
        lines.append(f"Reference: <code>{escape(i.reference)}</code>")
    if i.supplier_invoice_number:
        inv = f"Invoice {escape(i.supplier_invoice_number)}"
        if i.supplier_invoice_date:
            inv += f" of {i.supplier_invoice_date:%d.%m.%Y}"
        if i.due_date:
            inv += f", due <b>{i.due_date:%d.%m.%Y}</b>"
        lines.append(inv)
    if i.note:
        lines.append(escape(i.note))
    return "\n".join(lines)


def instruction_approved(i, payer) -> str:  # noqa: ANN001
    return (
        f"✅ Approved internally. Now make the transfer in the <b>Paynetics panel of "
        f"{escape(payer.code)}</b> with exactly these details and confirm it with SMS/push.\n"
        f"I'll mark instruction #{i.id} as executed when the bank shows the payment."
    )


def instruction_executed(i, payer) -> str:  # noqa: ANN001
    return (
        f"✅ <b>Payment executed</b>: {escape(payer.code)} → {escape(i.to_name)} "
        f"{fmt_money(i.amount, i.currency)}"
        + (f" (invoice {escape(i.supplier_invoice_number)})" if i.supplier_invoice_number else "")
        + f". Instruction #{i.id} closed."
    )


def instructions_list(rows) -> str:  # noqa: ANN001
    out = ["<b>Open payment instructions</b>"]
    for i, code in rows:
        due = f", due {i.due_date:%d.%m}" if i.due_date else ""
        out.append(
            f"#{i.id} {escape(code)} → {escape(i.to_name[:28])} · "
            f"{fmt_money(i.amount, i.currency)} · {escape(i.status.value)}{due}"
        )
    return "\n".join(out)
