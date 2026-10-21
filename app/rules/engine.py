"""Flow C: "X receives Y -> Z invoices X for Y - p%" (INVOICE_ON_RECEIPT).

Runs when an incoming transfer to the trigger company is first processed. It only creates a
DRAFT invoice; a human approves it in Telegram like any other draft.
"""

import logging
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.audit import audit
from app.companies import counterparty_for_company
from app.config import Settings
from app.db.models import (
    BankTransfer,
    Company,
    CompanyBankAccount,
    Contract,
    InvoiceSource,
    Rule,
    RuleRun,
    RuleType,
    TransferDirection,
)
from app.invoices import service as svc
from app.money import CENT

log = logging.getLogger(__name__)
DEFAULT_PERCENT = Decimal("1.5")


@dataclass
class RuleDraft:
    rule_id: int
    invoice_id: int
    received: Decimal
    percent: Decimal
    amount: Decimal


def amount_after_fee(received: Decimal, percent: Decimal) -> Decimal:
    """Y - p% of Y, rounded half-up to cents (10,000.00 at 1.5% -> 9,850.00)."""
    return (received * (100 - percent) / 100).quantize(CENT, rounding=ROUND_HALF_UP)


def _norm(s: str | None) -> str:
    return re.sub(r"[^0-9A-Z]", "", (s or "").upper())


async def is_internal(session: AsyncSession, bt: BankTransfer) -> bool:
    """Paid by another of our companies (by account number/IBAN or by exact company name)."""
    payer_acc, payer_name = _norm(bt.payer_account), _norm(bt.payer_name)
    accounts = (
        await session.execute(
            select(
                CompanyBankAccount.company_id,
                CompanyBankAccount.iban,
                CompanyBankAccount.bank_account_ref,
            )
        )
    ).all()
    if payer_acc and any(
        payer_acc in {_norm(i), _norm(r)} and cid != bt.company_id for cid, i, r in accounts
    ):
        return True
    names = (await session.execute(select(Company.id, Company.name, Company.legal_name))).all()
    return bool(payer_name) and any(
        cid != bt.company_id and payer_name in {_norm(n), _norm(ln)} for cid, n, ln in names
    )


def rule_percent(rule: Rule) -> Decimal:
    raw = (rule.params or {}).get("percent")
    return Decimal(str(raw)) if raw not in (None, "") else DEFAULT_PERCENT


async def run_receipt_rules(
    session: AsyncSession, settings: Settings, bt: BankTransfer
) -> list[RuleDraft]:
    if (
        bt.direction != TransferDirection.INCOMING
        or bt.company_id is None
        or bt.status != "processed"
    ):
        return []
    rules = (
        await session.scalars(
            select(Rule)
            .where(
                Rule.type == RuleType.INVOICE_ON_RECEIPT,
                Rule.enabled.is_(True),
                Rule.trigger_company_id == bt.company_id,
            )
            .order_by(Rule.id)
        )
    ).all()
    if not rules:
        return []
    internal = await is_internal(session, bt)
    drafts: list[RuleDraft] = []
    for rule in rules:
        if await session.scalar(
            select(RuleRun.id).where(RuleRun.rule_id == rule.id, RuleRun.transfer_id == bt.id)
        ):
            continue
        params = rule.params or {}
        if internal and not params.get("include_internal"):
            session.add(
                RuleRun(
                    rule_id=rule.id,
                    transfer_id=bt.id,
                    status="skipped",
                    detail="paid by one of our companies",
                )
            )
            continue
        percent = rule_percent(rule)
        amount = amount_after_fee(bt.amount, percent)
        run = RuleRun(rule_id=rule.id, transfer_id=bt.id, status="failed")
        session.add(run)
        try:
            issuer = await session.scalar(
                select(Company)
                .options(selectinload(Company.bank_accounts))
                .where(Company.id == rule.target_company_id)
            )
            trigger = await session.get(Company, rule.trigger_company_id)
            assert issuer is not None and trigger is not None
            client = await counterparty_for_company(session, trigger)
            contract = (
                await session.get(Contract, params["contract_id"])
                if params.get("contract_id")
                else None
            )
            description = (contract.service_description if contract else None) or "Services"
            if amount <= 0:
                raise svc.InvoiceError("computed amount is not positive")
            inv = await svc.create_draft(
                session,
                settings,
                issuer=issuer,
                client=client,
                lines=[svc.LineInput(description, Decimal(1), amount)],
                currency=bt.currency,
                include_vat=True,
                due_in_days=contract.payment_terms_days if contract else None,
                source=InvoiceSource.RULE,
            )
            if contract and contract.template_id:
                inv.template_id = contract.template_id
        except svc.InvoiceError as e:
            run.detail = str(e)
            audit(
                session,
                "system",
                "rule.failed",
                f"rule:{rule.id}",
                {"transfer": bt.external_id, "error": str(e)},
            )
            log.warning("rule %s failed for transfer %s: %s", rule.id, bt.external_id, e)
            continue
        run.status, run.invoice_id = "drafted", inv.id
        audit(
            session,
            "system",
            "rule.drafted",
            f"rule:{rule.id}",
            {
                "transfer": bt.external_id,
                "received": str(bt.amount),
                "percent": str(percent),
                "invoice": inv.id,
                "amount": str(amount),
            },
        )
        drafts.append(RuleDraft(rule.id, inv.id, bt.amount, percent, amount))
    return drafts
