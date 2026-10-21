"""Bring bank transfers into the DB and act on new incoming payments (Flow B).

Sources: the polling loop, the Paynetics webhook (a hint: the transfer is always re-fetched)
and the mock bank's /dev_simulate_* commands. Processing is idempotent: a transfer is acted on
once (BankTransfer.processed_at) and has at most one PaymentMatch.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit
from app.bank.base import BankProvider, Transfer
from app.bank.paynetics import PayneticsProvider
from app.config import Settings
from app.db.models import (
    BankTransfer,
    Company,
    CompanyBankAccount,
    Counterparty,
    Invoice,
    InvoiceStatus,
    MatchConfidence,
    PaymentMatch,
    TransferDirection,
)
from app.db.session import Database
from app.payments.matching import Candidate, MatchResult, match_transfer
from app.rules.engine import RuleDraft, run_receipt_rules

log = logging.getLogger(__name__)
_sync_lock = asyncio.Lock()
_OPEN = (InvoiceStatus.APPROVED, InvoiceStatus.SENT)
_FINAL_NO_ACTION = {"canceled", "cancelled", "reversed"}


@dataclass
class PaymentEvent:
    kind: str  # "paid" (auto, HIGH) | "review" (needs a human) | "rule_draft"
    transfer_id: int
    company_id: int | None
    result: MatchResult | None = None
    candidates: list[Candidate] = field(default_factory=list)
    rule_draft: RuleDraft | None = None


def _norm(acc: str | None) -> str:
    return re.sub(r"\s+", "", acc or "").upper()


async def _accounts_by_code(session: AsyncSession) -> dict[str, set[str]]:
    rows = (
        await session.execute(
            select(Company.code, CompanyBankAccount.bank_account_ref, CompanyBankAccount.iban).join(
                CompanyBankAccount, CompanyBankAccount.company_id == Company.id
            )
        )
    ).all()
    out: dict[str, set[str]] = {}
    for code, ref, iban in rows:
        out.setdefault(code, set()).update(x for x in (ref, iban) if x)
    return out


async def _resolve_company(session: AsyncSession, t: Transfer) -> Company | None:
    if t.owner_code:
        found = await session.scalar(select(Company).where(Company.code == t.owner_code))
        if found:
            return found
    if not t.account:
        return None
    for acc in (await session.scalars(select(CompanyBankAccount))).all():
        if _norm(t.account) in {_norm(acc.bank_account_ref), _norm(acc.iban)}:
            return await session.get(Company, acc.company_id)
    return None


async def upsert_transfer(session: AsyncSession, provider: str, t: Transfer) -> BankTransfer:
    bt = await session.scalar(
        select(BankTransfer).where(
            BankTransfer.provider == provider, BankTransfer.external_id == t.external_id
        )
    )
    company = await _resolve_company(session, t)
    if bt is None:
        bt = BankTransfer(provider=provider, external_id=t.external_id)
        session.add(bt)
    bt.direction, bt.status, bt.amount, bt.currency = t.direction, t.status, t.amount, t.currency
    bt.payer_name, bt.payer_account = t.payer_name, t.payer_account
    bt.beneficiary_name, bt.beneficiary_account = t.beneficiary_name, t.beneficiary_account
    bt.reference, bt.booked_at, bt.raw_json = t.reference, t.booked_at, t.raw
    bt.account = t.account
    bt.company_id = company.id if company else bt.company_id
    await session.flush()
    return bt


async def open_candidates(session: AsyncSession, company_id: int) -> list[Candidate]:
    rows = (
        await session.execute(
            select(Invoice, Counterparty)
            .join(Counterparty, Counterparty.id == Invoice.counterparty_id)
            .where(Invoice.issuer_id == company_id, Invoice.status.in_(_OPEN))
            .order_by(Invoice.id)
        )
    ).all()
    return [Candidate(inv, client) for inv, client in rows]


async def mark_paid(
    session: AsyncSession, invoice: Invoice, bt: BankTransfer, actor: str, method: str
) -> None:
    invoice.status = InvoiceStatus.PAID
    invoice.related_transfer_id = bt.id
    audit(
        session,
        actor,
        "invoice.paid",
        f"invoice:{invoice.id}",
        {
            "number": invoice.number,
            "transfer": bt.external_id,
            "amount": str(bt.amount),
            "method": method,
        },
    )


async def process_transfer(
    session: AsyncSession, bt: BankTransfer, settings: Settings | None = None
) -> list[PaymentEvent]:
    if bt.processed_at is not None:
        return []
    if bt.status in _FINAL_NO_ACTION:
        bt.processed_at = datetime.now(UTC)
        return []
    if bt.status != "processed":
        return []  # new/pending: look again on the next sync
    bt.processed_at = datetime.now(UTC)
    if bt.direction != TransferDirection.INCOMING or bt.company_id is None:
        return []  # outgoing transfers are matched to payment instructions in M5
    events = [await _match(session, bt)]
    if settings is not None:
        for d in await run_receipt_rules(session, settings, bt):
            events.append(PaymentEvent("rule_draft", bt.id, bt.company_id, rule_draft=d))
    return events


async def _match(session: AsyncSession, bt: BankTransfer) -> PaymentEvent:
    candidates = await open_candidates(session, bt.company_id)
    result = match_transfer(
        bt.amount, bt.currency, bt.reference, bt.payer_name, bt.payer_account, candidates
    )
    auto = result.confidence == MatchConfidence.HIGH and result.invoice is not None
    session.add(
        PaymentMatch(
            transfer_id=bt.id,
            invoice_id=result.invoice.id if auto else None,
            confidence=result.confidence,
            method=result.method,
        )
    )
    if auto:
        await mark_paid(session, result.invoice, bt, "system", result.method)
    audit(
        session,
        "system",
        "transfer.matched",
        f"transfer:{bt.id}",
        {
            "external_id": bt.external_id,
            "confidence": result.confidence.value,
            "method": result.method,
            "invoice": result.invoice.number if result.invoice else None,
        },
    )
    return PaymentEvent(
        "paid" if auto else "review", bt.id, bt.company_id, result, result.candidates
    )


async def ingest(
    db: Database, provider: str, transfers: list[Transfer], settings: Settings | None = None
) -> list[PaymentEvent]:
    events: list[PaymentEvent] = []
    for t in transfers:
        async with db.session() as s:
            bt = await upsert_transfer(s, provider, t)
            events += await process_transfer(s, bt, settings)
            await s.commit()
    return events


async def sync(
    db: Database, bank: BankProvider, since: datetime, settings: Settings | None = None
) -> list[PaymentEvent]:
    async with _sync_lock:
        if isinstance(bank, PayneticsProvider):
            async with db.session() as s:
                bank.accounts_by_code = await _accounts_by_code(s)
        transfers = await bank.list_transfers(since)
        return await ingest(db, bank.name, transfers, settings)


async def sync_one(
    db: Database, bank: BankProvider, transfer_id: str, settings: Settings | None = None
) -> list[PaymentEvent]:
    """Webhook/dev path: always re-fetch from the bank, never trust the pushed payload."""
    async with _sync_lock:
        if isinstance(bank, PayneticsProvider):
            async with db.session() as s:
                bank.accounts_by_code = await _accounts_by_code(s)
        views = getattr(bank, "get_transfer_views", None)
        transfers = await views(transfer_id) if views else [await bank.get_transfer(transfer_id)]
        return await ingest(db, bank.name, transfers, settings)


async def load_for_message(session: AsyncSession, ev: PaymentEvent):  # noqa: ANN201
    bt = await session.get(BankTransfer, ev.transfer_id)
    company = await session.get(Company, ev.company_id) if ev.company_id else None
    return bt, company
