# invoice-payment-bot

Telegram bot + FastAPI backend that prepares invoices and payment instructions between related
companies and tracks money movement through the (read-only) Paynetics API. Money is never moved
from code: transfers are executed by a human in the bank panel. See `CLAUDE.md` for the full brief.

## Status

- **M1 (done):** config, database, company profiles (with multiple bank accounts and invoice
  templates per company), Telegram bot with whitelist, mock bank, `/dev_*` simulation commands.
- M2 next: invoice drafting (Claude parser), templates → PDF, approve/send flow (dry run).

## Run locally

```bash
uv venv -p python3.12 .venv
uv pip install -p .venv/bin/python -e ".[dev]"

cp .env.example .env                                 # set TELEGRAM_BOT_TOKEN, BOOTSTRAP_APPROVER_IDS
cp config/companies.example.yaml config/companies.yaml   # real company profiles (gitignored)

.venv/bin/uvicorn app.main:app --port 8000
curl localhost:8000/health
```

On startup the app creates the SQLite schema, inserts any companies from `config/companies.yaml`
that are not in the DB yet (it never overwrites existing ones), and whitelists
`BOOTSTRAP_APPROVER_IDS` as approvers. Your Telegram user id: message @userinfobot.

## Bot commands (M1)

| Command | |
|---|---|
| `/start`, `/help` | |
| `/companies` | company profiles and bank accounts (IBANs redacted) |
| `/balance <company>` | balance of the company's default account |
| `/dev_simulate_incoming <company> <amount> <payer> [reference]` | mock bank only |
| `/dev_simulate_outgoing <company> <amount> <beneficiary> [iban] [reference]` | mock bank only |

Non-whitelisted users are ignored silently.

## Tests

```bash
.venv/bin/pytest -q
.venv/bin/ruff check app tests
```
