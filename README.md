# invoice-payment-bot

Telegram bot + FastAPI backend that prepares invoices and payment instructions between related
companies and tracks money movement through the (read-only) Paynetics API. Money is never moved
from code: transfers are executed by a human in the bank panel. See `CLAUDE.md` for the full brief.

## Status

- **M1 (done):** config, database, company profiles (with multiple bank accounts and invoice
  templates per company), Telegram bot with whitelist, mock bank, `/dev_*` simulation commands.
- **Admin panel (done):** `/admin` — company profiles, logo upload, bank accounts (IBAN
  checksum), invoice templates per company (3 layouts × colour/font/blocks/notes, duplicate,
  default, archive) with a live preview, note presets.
- **M2 (done):** invoices from Telegram. Write e.g. *"Invoice Client Ltd 1200 EUR from Z,
  consulting"* → Claude turns it into a draft (asks with buttons when the company or client is
  unclear) → draft PDF with **Approve / Edit / Reject** → approval assigns the next number (no
  gaps), freezes issuer/client/bank data, stores the PDF and creates a secret client link
  (`/i/<token>`) → **Send to client** delivers by the client's channels (email and/or
  Telegram). In `DRY_RUN` nothing reaches the client: email goes to `DRY_RUN_EMAIL` or is only
  logged, Telegram goes to the operator, marked `[DRY RUN]`. Admin panel: **Clients** (profile +
  delivery channels) and **Invoices** (PDFs, copy client link).
- **M3 (done):** read-only Paynetics, one account (and API token) per company. Every
  `POLL_INTERVAL_SECONDS` the app pulls each company's transfers for the last
  `POLL_LOOKBACK_HOURS`, caches them, and matches incoming payments to open invoices
  (invoice number in the payment reason → amount + payer → amount only). Certain matches mark
  the invoice **PAID** and notify in Telegram; unclear ones ask an approver with buttons.
  `POST /webhooks/paynetics` is a hint only (the transfer is always re-fetched). `/payments`
  lists recent transfers. Admin → company → **Bank API → Test connection** shows the
  company's Paynetics accounts and which are linked.
- **M4 (done):** rules (Admin → **Rules**): "when X receives a payment of Y, Z invoices X for
  Y − p%" (default 1.5%, half-up to cents) with the contract's service description as the
  invoice line. The draft goes to Telegram with Approve / Edit / Reject. Payments from our own
  companies do not trigger rules unless enabled per rule; each payment fires a rule at most once
  (`rule_runs`). `/rules` lists them.
- **Documents (done):** send a photo/PDF to the bot and pick **Issue a similar invoice**,
  **Save as client**, **Create/update our company** (shown for confirmation first) or
  **Invoice to pay**; a caption like "pay this" picks the action directly.
- **M5 part 1 (done): payment instructions.** "Invoice to pay" reads supplier, IBAN (checksum),
  amount, reference and due date, resolves which of our companies pays, and shows a card with
  **Approve payment / Cancel** (no duplicates per supplier invoice). After approval you pay in the Paynetics panel; the outgoing transfer (same
  payer, amount, currency + IBAN or reference) marks it **EXECUTED** and notifies. Invoices
  from our own companies are cross-checked with what we issued. `/instructions` lists open ones.
- **M5 part 2 (done): payout rules (Flow E).** Admin → Rules → **+ Payout rule**: "when Z
  receives a payment (optionally only from X), pay G a % / a fixed amount / all but an amount
  kept". The bot proposes the payment instruction (to G's default account in the currency, or
  a fixed external IBAN) with a reference from a template; approval and execution are the same
  as above. The full X → Z → G cycle runs end to end in mock mode.
- **M6 (done): hardening.**
  - Above `MAX_INVOICE_AMOUNT` / `MAX_PAYMENT_INSTRUCTION_AMOUNT` nothing is blocked, but
    approval needs **two different approvers**: the first approval is recorded (no invoice
    number is used) and the other approvers of that company get the item with its buttons.
  - Reminders in Telegram: invoices past their due date that aren't paid, and approved
    payment instructions the bank hasn't shown after `INSTRUCTION_TIMEOUT_HOURS`; repeated
    at most every `REMINDER_REPEAT_HOURS`.
  - Admin → **Team**: who may use the bot (Telegram id, name, approver/operator, companies).
  - Admin → **Audit log**: filter by action/entity/actor/date, **Export CSV**.
- **Client invoice requests (done):** Admin → client → **Invoice requests → Create request
  link** gives a secret link (`/r/<token>`, new link / disable any time, optionally limited to
  some of our companies). The client picks which of our companies invoices them, the currency
  and the lines, and sees the status of their requests (with the invoice PDF once issued) on
  the same page. Requests arrive in Telegram (also `/requests`) with **Create draft /
  Decline**; the draft is approved as usual. At most 5 open requests per client.
- **Finance (done):** Admin → **Finance**: period (this/last month, 30 days, year, custom),
  company and currency filters; live balances; received / paid out / net; invoiced, paid and
  still due; what clients owe now with aging (not due, 1–30, 31–60, 60+ days late); open
  payment instructions; every transfer with what it is tied to; CSV export. **Load bank
  history** fetches up to a year of Paynetics transfers for reporting only (no rules or
  notifications for old payments).
- **Partial payments (done):** several payments can settle one invoice; matching compares
  with what is still due; the invoice becomes PAID when the remainder reaches 0 (overpayments
  are shown). Telegram says "partial payment, X still due".
- **Suggestions (done):** on the Finance page, with one-click actions: a payment that is
  probably for invoice N (and why), payments that add up to an invoice, invoices left open by
  a few cents of bank charges, payments we have to make in the next 7 days.
- **/report (done):** `/report [last|30|year|YYYY-MM]` summary in Telegram with a link to
  Finance; a weekly summary goes to approvers on Mondays at 08:00 (`WEEKLY_DIGEST_ENABLED`,
  `DIGEST_WEEKDAY`, `DIGEST_HOUR`, `TIMEZONE`).
- **Credit notes (done):** `/credit <invoice number> [reason]` drafts a credit note for the full
  amount (negative lines, same parties as the invoice) with Approve / Reject. Approval gives it
  the next number in the issuer's series and cancels the invoice (a PAID one stays PAID; the
  refund is made in the bank). Delivery works like an invoice. *Confirm the numbering and
  wording with your accountant.*

### Connecting Paynetics

1. For each company, in its Paynetics panel (master user): Settings → API Tokens → Create.
   Webhook URL: `https://<your-app>/webhooks/paynetics`. Copy the Secret-Key (shown once).
2. In Render → Environment add, per company code (upper case, `-` → `_`):
   `PAYNETICS_<CODE>_ACCESS_TOKEN` and `PAYNETICS_<CODE>_SECRET_KEY`, then set
   `BANK_PROVIDER=paynetics`.
3. Admin → company → Bank API → **Test connection**; put each returned account number into the
   matching bank account's "Bank API account number".

## Run locally

```bash
uv venv -p python3.12 .venv
uv pip install -p .venv/bin/python -e ".[dev]"

cp .env.example .env    # set TELEGRAM_BOT_TOKEN, BOOTSTRAP_APPROVER_IDS, ADMIN_PASSWORD, SESSION_SECRET
cp config/companies.example.yaml config/companies.yaml   # real company profiles (gitignored)

.venv/bin/uvicorn app.main:app --port 8000
curl localhost:8000/health
```

On startup the app creates the SQLite schema, inserts any companies from `config/companies.yaml`
that are not in the DB yet (it never overwrites existing ones), and whitelists
`BOOTSTRAP_APPROVER_IDS` as approvers. Your Telegram user id: message @userinfobot.

Admin panel: open http://localhost:8000/admin and log in with `ADMIN_PASSWORD`. It is disabled
while `ADMIN_PASSWORD` is empty. In production set `SESSION_SECRET` (long random string) and
`HTTPS_ONLY_COOKIES=true`.

## Deploy on Render

`render.yaml` is a Render Blueprint: one web service (Frankfurt, `starter` plan) with a 1 GB
persistent disk at `/var/data` for the SQLite database and uploaded logos. In the Render
dashboard: **New → Blueprint → pick this repo**, enter `ADMIN_PASSWORD` when asked (leave
`TELEGRAM_BOT_TOKEN` / `BOOTSTRAP_APPROVER_IDS` empty until the bot is ready), then **Apply**.
`SESSION_SECRET` is generated by Render. Every push to `main` redeploys.

Schema changes go through Alembic (`migrations/`); the app runs `alembic upgrade head` on
startup. After changing a model: `alembic revision --autogenerate -m "..."`, review the file,
and test it against a database that already has rows (see CLAUDE.md of the sibling project for
why). `tests/test_migrations.py` fails if models and migrations drift apart.

`/health` reports the PDF engine (`pdf`), whether the Claude key (`llm`) and SMTP (`email`) are
configured. Only approvers can approve; operators can draft.

## Bot commands

| Command | |
|---|---|
| `/start`, `/help` | |
| `/companies` | company profiles and bank accounts (IBANs redacted) |
| *any text* | draft an invoice from free text (needs `ANTHROPIC_API_KEY`) |
| `/invoices` | last 10 invoices, with Send buttons for approved ones |
| `/payments` | check the bank now and list recent transfers with their match |
| `/rules` | automatic invoicing rules |
| `/instructions` | open payment instructions (payments we have to make) |
| *photo / PDF* | choose: similar invoice, save client, our company, invoice to pay |
| `/cancel` | stop editing a draft |
| `/balance <company>` | balance of the company's default account |
| `/dev_simulate_incoming <company> <amount> <payer> [reference]` | mock bank only |
| `/dev_simulate_outgoing <company> <amount> <beneficiary> [iban] [reference]` | mock bank only |

Non-whitelisted users are ignored silently.

## Tests

```bash
.venv/bin/pytest -q
.venv/bin/ruff check app tests
```
