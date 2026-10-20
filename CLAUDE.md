# Project: Invoice & Payment Flow Bot (MVP)

Telegram bot + backend that automates an invoicing and payment cycle between related companies (X, Z, G), using the Paynetics read-only API for money movement detection. **Goal of this phase: a fast, testable prototype that shows how far we can get with what exists today.**

Docs for the bank API: `docs/openapi-paynetics.yaml` (read it before writing the Paynetics client; do not guess schemas).

Language: **everything is in English** — code, comments, commit messages, Telegram messages, invoice PDFs and emails. (The original brief said Bulgarian for user-facing text; the user changed this on 2026-10-20.) Keep all user-facing strings in one place (`bot/texts.py`, email/invoice templates) so a translation layer can be added later if needed. Default currency is EUR (configurable per company).

---

## 1. Hard constraints (read first)

1. **The Paynetics API is READ-ONLY.** No endpoint creates transfers. Transfers are executed manually by a human in the Paynetics panel (SCA via SMS/push). Never try to bypass this.
2. **Never automate the bank portal** (no scraping, RPA, headless browser, notification listeners, OTP/SMS/TOTP reading or relaying). Never handle, store or forward SCA codes or bank credentials.
3. **Never move money from code.** The bot only prepares *payment instructions* and tracks their status.
4. **The LLM never triggers actions.** Claude API only parses free text into a structured draft (validated with pydantic). Every side effect (send email, create payment instruction) requires an explicit human approval button in Telegram.
5. **DRY_RUN=true by default.** In dry run, emails are not sent to real recipients (redirect to `DRY_RUN_EMAIL` or just log), and the bank provider may be the mock.
6. Money is `Decimal` (or integer cents). Never float. Rounding: half-up to 2 decimals.
7. Secrets only in `.env` (gitignored) and loaded via pydantic-settings. Provide `.env.example`.
8. Webhooks from Paynetics are **unsigned** and retried only once: treat them as a *hint*, always re-fetch the transfer via `GET /v1/transfers/{id}` before acting. Also run a polling fallback.
9. Incoming emails are untrusted (spoofable). They are signals that trigger a Telegram notification for human confirmation, never final proof.

---

## 2. Stack

- Python 3.12, FastAPI, aiogram 3 (long polling for Telegram in MVP)
- SQLAlchemy 2 + SQLite (keep models Postgres-compatible), Alembic optional
- httpx (async) for Paynetics and Claude API
- Jinja2 + WeasyPrint for invoice HTML → PDF
- SMTP for sending, IMAP for reading (per-company credentials in config; Gmail API can replace later)
- pydantic v2, pydantic-settings, pytest, ruff
- APScheduler (or asyncio tasks) for polling jobs
- Claude API for text parsing: model from env `CLAUDE_MODEL` (default `claude-sonnet-5-5`), force JSON output, strip code fences, validate with pydantic, retry once on invalid output

Suggested layout:

```
app/
  main.py                # FastAPI app + startup (bot, scheduler)
  config.py
  db/ (models.py, session.py)
  bot/ (handlers.py, keyboards.py, texts.py, auth.py)
  llm/parser.py          # free text -> InvoiceDraft
  invoices/ (service.py, numbering.py, render.py, templates/invoice.html.j2)
  email/ (sender.py, inbox.py, templates/*.j2)
  bank/
    base.py              # BankProvider protocol
    paynetics.py         # real read-only client
    mock.py              # simulated bank for tests / demo
  payments/ (matching.py, instructions.py)
  rules/engine.py        # Y -> Y-1.5% invoice rule, payout-to-G rule
  webhooks.py            # POST /webhooks/paynetics
  audit.py
tests/
docs/openapi-paynetics.yaml
```

---

## 3. Data model (minimum)

- `Company`: id, name, vat_id/EIK, address, iban, bank_account_ref (Paynetics account), email_from, smtp/imap config ref, invoice_prefix/sequence, default_currency
- `Counterparty` (client): name, EIK, VAT, address, email, iban (nullable)
- `Contract`: company_id (issuer), counterparty_id, service_description (portfolio text for invoices/emails), fee_rule (percent/fixed), payment_terms_days
- `Invoice`: number, issuer_id, client (counterparty or company), amount_net, vat_rate, amount_gross, currency, issue_date, due_date, status (`DRAFT → APPROVED → SENT → PAID | CANCELLED`), pdf_path, source (`manual`/`rule`), related_payment_id
- `BankTransfer` (cached from bank): external_id, direction, status, amount, currency, payer/beneficiary name + IBAN, reason/reference, booked_at, raw_json
- `PaymentMatch`: transfer_id, invoice_id, confidence (`HIGH/MEDIUM/LOW`), method, confirmed_by_user
- `PaymentInstruction`: id, from_company, to_iban, to_name, amount, currency, reference, linked_invoice_id / linked_rule, status (`PROPOSED → APPROVED → AWAITING_BANK → EXECUTED | CANCELLED`), matched_transfer_id
- `Rule`: type (`INVOICE_ON_RECEIPT`, `PAYOUT_ON_RECEIPT`), trigger_company, target_company, params JSON (percent, fixed, remainder), enabled
- `AuditLog`: ts, actor (telegram user id / system), action, entity, payload JSON
- `AllowedUser`: telegram_id, role (`operator`/`approver`), companies allowed

---

## 4. Bank provider abstraction

```python
class BankProvider(Protocol):
    async def get_balance(self, account: str) -> Balance: ...
    async def list_transfers(self, since: datetime, direction: str | None = None) -> list[BankTransfer]: ...
    async def get_transfer(self, transfer_id: str) -> BankTransfer: ...
    async def get_statement(self, account: str, date_from, date_to) -> list[BankTransfer]: ...
    # Not supported by Paynetics today:
    async def create_transfer(self, instr: PaymentInstruction) -> None:
        raise NotSupported  # -> falls back to ManualExecution (Telegram card)
```

- `PayneticsProvider`: base URL `https://api.paynetics.online/v1`, headers `Access-Token` and `Secret-Key`. Implement from the OpenAPI file: `/profile`, `/accounts/{account}`, `/transfers`, `/transfers/{transferId}`, `/statements/{account}/`. Respect real parameter names/enums from the spec. Handle 401, 429/5xx with backoff.
- **Facts from the spec (v1.2.0) that differ from what the brief assumed** — code to these, not to the brief:
  - `GET /v1/transfers` has no "since"/date-range or account filter. Query params are only `type` (`all|incoming|outgoing|internal`), `status` (`all|new|pending|processed|canceled|reversed`), `date` (a single day, `YYYY-MM-DD`), `amount`, `page`. So `list_transfers(since=...)` must iterate day by day and page by page. It returns transfers for every account the token can see.
  - List response: `{"data": {"status", "transfers": [...], "pagination": {...}}}`. Empty result comes back as **HTTP 404** `{"message": "No account found"}` / "No transfers found" — treat as empty, not as an error.
  - Detail response: `{"status": "success", "transfer-details": [ {transfer} ]}` (array, one element).
  - Transfer fields: `transferId, transferType, payer, payerAccount, beneficiary, beneficiaryAccount, amount (string), currency, paymentDetails, transferStatus, created, updated`. There is no separate IBAN or reference field: `*Account` may be an internal account number (`41411111111`) or an IBAN; `paymentDetails` is the payment reason/reference. The `created`/`updated` format is undocumented — confirm from a real response before relying on it.
  - Balance: `GET /v1/accounts/{account}` → `data.account {accountNumber, accountName, currency, balance, availableBalance, freezeStatus}` (amounts are strings).
  - Statements: `GET /v1/statements/{account}/?dateStart&dateEnd&page` (range ≤ 1 year) → `statements[] {debit, credit, currency, paymentDetails, accountHolder, accountNumber, transferId, orderDate, processedDate}` + `totals`.
  - Errors: 400, 401 (no reason in body), 403, 404, 502. 429 is not documented — still back off on 429/5xx.
  - Webhook `POST` body: `{payer, amount, currency, transferId, beneficiary, payerAccount, transferStatus, beneficiaryAccount}`, unsigned, retried once.
  - Ignore `/v1/register*`, `/v1/cards*` (returns card PAN/CVV — never call it), `/v1/rates`.
- `MockBankProvider`: in-memory/SQLite, plus Telegram dev command `/dev_simulate_incoming <company> <amount> <payer> [reference]` and `/dev_simulate_outgoing ...` to run the full flow with no bank.
- Select with `BANK_PROVIDER=mock|paynetics`.

When the bank later adds "transfer waiting for confirmation" via API, only `create_transfer` needs to be implemented; the rest of the flow stays.

---

## 5. Flows to implement

### Flow A: Issue invoice (manual command)
1. Operator writes, e.g. "Issue an invoice to Client Ltd for 1200 EUR from Company Z, service: consulting services".
2. `llm/parser.py` returns `InvoiceDraft` (issuer, client, amount, vat?, description, currency, due date). If a field is ambiguous or unknown (company not found), ask the operator in Telegram with buttons rather than guessing.
3. Bot renders PDF from the approved template and sends it back with buttons **[Approve] [Edit] [Reject]**.
4. On approve → status APPROVED; then bot asks "Send it now?" **[Send] [Not now]**.
5. On send → email from the issuer's mailbox with English subject/body templates, PDF attached; status SENT. Audit log everything.

### Flow B: Incoming payment detection
1. Poll `list_transfers` (incoming, last N hours) every `POLL_INTERVAL_SECONDS` (default 120) and handle `/webhooks/paynetics` as a hint (then refetch).
2. Matching order: invoice number found in reason → exact amount + payer IBAN/name vs client → exact amount among open invoices of that issuer → ambiguous.
3. HIGH confidence: mark invoice PAID and notify "Received X from Y for invoice N". MEDIUM/LOW: ask in Telegram to choose the invoice or mark as unmatched.
4. Idempotent: the same transfer is never processed twice.

### Flow C: Rule "X receives Y → Z invoices X for Y − 1.5%"
1. After an incoming payment to company X is confirmed, check enabled `INVOICE_ON_RECEIPT` rules.
2. Compute `amount = Y * (1 - percent)` (percent configurable, default 1.5%), create a DRAFT invoice from Z to X using the `Contract` service description, and send it for approval (same buttons as Flow A).
3. After approval, Z sends it by email to X.

### Flow D: X receives Z's invoice → payment instruction
1. IMAP poll on X's mailbox. If an email with an invoice (PDF) from Z's known address arrives, notify in Telegram: "Invoice received from Z for N". Extract amount/number with the LLM parser (validate against the invoice Z issued if it exists in our DB).
2. Operator confirms **[Confirm invoice]**.
3. Bot creates a `PaymentInstruction` (X → Z): IBAN, amount, reference = invoice number. Telegram card shows all fields in copyable monospace plus a **[Approved internally]** button, then "Execute it in the Paynetics panel and confirm via SMS/push". Optional: export a CSV for panel import if supported.
4. Polling detects the outgoing transfer (match by IBAN + amount + reference) → instruction EXECUTED, notify. If not seen in `INSTRUCTION_TIMEOUT_HOURS`, remind.

### Flow E: Z receives payment → payout to account G
1. When Z's incoming payment from X is confirmed, check `PAYOUT_ON_RECEIPT` rules (params: percent / fixed / remainder, destination IBAN G, reference template).
2. Create a `PaymentInstruction` (Z → G), same manual flow as Flow D step 3–4.

### Telegram commands
`/start`, `/help`, `/companies`, `/invoices` (list by status), `/payments` (recent transfers + match status), `/instructions` (open payment instructions), `/rules`, `/balance <company>`, `/dev_*` (only in mock mode).

---

## 6. Invoice template and rules
- HTML/Jinja2 template in `invoices/templates/invoice.html.j2`, styled for A4, English labels (Invoice, Supplier, Customer, Company ID (EIK), VAT number, Description, Taxable amount, VAT, Total due, Bank account, Due date).
- Sequential numbering per issuer, 10 digits, no gaps (**verify requirements with the client's accountant; do not hardcode legal claims**). VAT rate configurable per company/contract; support "no VAT" with a configurable legal note.
- The client will provide the approved template later; keep the template swappable (path in config).

---

## 7. Safety, limits, audit
- Telegram whitelist (`AllowedUser`); ignore everyone else silently.
- Amount limits in config: `MAX_INVOICE_AMOUNT`, `MAX_PAYMENT_INSTRUCTION_AMOUNT`. Above the limit require a second approver.
- Every state change writes `AuditLog`.
- Idempotency keys for invoice sending and for processing bank transfers.
- Log without secrets. Redact IBANs partially in logs.
- Verify SPF/DKIM results in incoming email headers when present; otherwise mark "unverified".

---

## 8. Milestones (build in this order, each one runnable)

1. **M1 Skeleton + mock bank**: config, DB, Telegram bot with whitelist, `/dev_*` commands. *Done when:* bot starts, whitelisted user gets replies.
2. **M2 Invoices**: parser, template, PDF, approve buttons, send via SMTP (dry run). *Done when:* the Flow A text command produces a PDF for approval and a logged email.
3. **M3 Paynetics read-only**: real provider, `/balance`, polling, transfer cache, matching, webhook hint endpoint. *Done when:* `/payments` shows real or mock transfers and a simulated incoming payment flips an invoice to PAID.
4. **M4 Rules**: Flow C (Y − 1.5%) with contract descriptions. *Done when:* a simulated payment to X generates a draft invoice from Z for the right amount.
5. **M5 Inbox + payment instructions**: Flows D and E, outgoing-transfer detection. *Done when:* the full cycle X → Z → G runs end to end in mock mode, and against the real Paynetics API for detection with manual execution.
6. **M6 Hardening**: limits, reminders, audit export, README with run instructions.

Write pytest tests for: amount math (1.5% rounding), matching logic (HIGH/MEDIUM/LOW/ambiguous), idempotency, LLM output validation failures, instruction state machine, and an end-to-end mock scenario.

---

## 9. Assumptions and open questions (do not block on them; make configurable)
- Currency EUR; VAT handling per accountant.
- Which mailboxes (Gmail/Microsoft/other) → SMTP/IMAP first.
- Invoice template and contract/portfolio texts will be provided by the client.
- Whether the Paynetics panel supports payment file import or deep links (asked the bank); until known, use copyable cards.
- Related-party invoicing between X, Z, G needs accountant sign-off for real use.

## 10. When starting
1. Read `docs/openapi-paynetics.yaml`.
2. Plan briefly, then build M1 and M2 first and show how to run them.
3. Ask me before adding anything not in this brief, and before touching real credentials or sending real emails.

## 11. Workflow conventions (from the user)
- **Never commit or push without the user's explicit "пушни го"** — even when a stop-hook or other automated check nudges toward committing.
- Build the real feature, not a mockup; verify it live (run it locally and exercise it) before calling a milestone done.
- Talk to the user in Bulgarian; everything in the repo is English.
