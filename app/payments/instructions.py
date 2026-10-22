"""Payment instructions (Flow D/E): what one of our companies must pay, prepared by the bot,
approved internally, executed by a human in the Paynetics panel, then detected by polling.

State machine: PROPOSED -> AWAITING_BANK (approved internally) -> EXECUTED, or -> CANCELLED.
Nothing here moves money.
"""

import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit
from app.config import Settings
from app.db.models import (
    BankTransfer,
    Company,
    InstructionStatus,
    Invoice,
    PaymentInstruction,
    TransferDirection,
)
from app.iban import iban_is_valid, normalize_iban
from app.llm.parser import DocumentRead
from app.money import fmt_money
from app.payments.matching import norm_company_name

OPEN = (InstructionStatus.PROPOSED, InstructionStatus.APPROVED, InstructionStatus.AWAITING_BANK)


class InstructionError(Exception):
    """A user-facing reason why an instruction can't be created or changed."""


def _norm_ref(s: str | None) -> str:
    return re.sub(r"[^0-9A-Z]", "", (s or "").upper())


async def create_from_document(
    session: AsyncSession,
    settings: Settings,
    *,
    payer: Company,
    doc: DocumentRead,
    document_path: str | None,
    created_by: int | None,
) -> PaymentInstruction:
    supplier = doc.issuer
    problems = []
    if not supplier.name:
        problems.append("supplier name")
    if doc.total is None or doc.total <= 0:
        problems.append("amount due")
    if not doc.currency:
        problems.append("currency")
    iban = normalize_iban(doc.issuer_iban or "")
    if not iban:
        problems.append("supplier IBAN")
    elif not iban_is_valid(iban):
        problems.append(f"a valid supplier IBAN (read {iban}, checksum fails)")
    if problems:
        raise InstructionError(
            "Couldn't read: " + ", ".join(problems) + ". Send a clearer photo or the PDF."
        )
    assert doc.total is not None and doc.currency is not None
    if doc.invoice_number:
        dup = await session.scalar(
            select(PaymentInstruction).where(
                PaymentInstruction.from_company_id == payer.id,
                PaymentInstruction.supplier_invoice_number == doc.invoice_number,
                PaymentInstruction.to_iban == iban,
                PaymentInstruction.status != InstructionStatus.CANCELLED,
            )
        )
        if dup:
            raise InstructionError(
                f"Invoice {doc.invoice_number} already has payment instruction #{dup.id} "
                f"({dup.status.value})."
            )
    instr = PaymentInstruction(
        from_company_id=payer.id,
        to_iban=iban,
        to_name=supplier.name,
        to_bic=(doc.issuer_bic or "").replace(" ", "").upper() or None,
        to_bank_name=doc.issuer_bank_name,
        amount=doc.total,
        currency=doc.currency,
        reference=(doc.payment_reference or doc.invoice_number or "")[:255],
        supplier_invoice_number=doc.invoice_number,
        supplier_invoice_date=doc.issue_date,
        due_date=doc.due_date,
        document_path=document_path,
        created_by=created_by,
        status=InstructionStatus.PROPOSED,
    )
    instr.note = await _link_our_invoice(session, instr, supplier.name)
    session.add(instr)
    await session.flush()
    audit(
        session,
        f"tg:{created_by}" if created_by else "system",
        "instruction.proposed",
        f"instruction:{instr.id}",
        {
            "payer": payer.code,
            "to": instr.to_name,
            "amount": str(instr.amount),
            "currency": instr.currency,
            "invoice": instr.supplier_invoice_number,
        },
    )
    return instr


async def _link_our_invoice(
    session: AsyncSession, instr: PaymentInstruction, supplier_name: str | None
) -> str | None:
    """When the supplier is one of OUR companies and we issued that invoice, link it and
    cross-check the amount (Flow D: validate against the invoice Z issued)."""
    if not instr.supplier_invoice_number:
        return None
    key = norm_company_name(supplier_name)
    companies = (await session.scalars(select(Company))).all()
    issuer = next(
        (
            c
            for c in companies
            if key and key in {norm_company_name(c.name), norm_company_name(c.legal_name)}
        ),
        None,
    )
    if issuer is None:
        return None
    inv = await session.scalar(
        select(Invoice).where(
            Invoice.issuer_id == issuer.id, Invoice.number == instr.supplier_invoice_number
        )
    )
    if inv is None:
        return (
            f"Supplier is our company {issuer.code}, but invoice {instr.supplier_invoice_number}"
            " is not in our records."
        )
    instr.linked_invoice_id = inv.id
    if inv.amount_gross != instr.amount or inv.currency != instr.currency:
        return (
            f"⚠️ Our records show invoice {inv.number} for "
            f"{fmt_money(inv.amount_gross, inv.currency)}, the document says "
            f"{fmt_money(instr.amount, instr.currency)}."
        )
    return f"Matches our invoice {inv.number} issued by {issuer.code}."


async def propose_payout(
    session: AsyncSession,
    settings: Settings,
    *,
    rule,
    payer: Company,  # noqa: ANN001
    target: Company | None,
    amount,
    currency: str,
    reference: str,  # noqa: ANN001
    iban: str | None,
    name: str | None,
) -> PaymentInstruction:
    """Flow E: an instruction from a payout rule (to one of our companies or a fixed IBAN)."""
    from app.companies import default_bank_account

    if iban:
        to_iban, to_name, bic, bank = normalize_iban(iban), name or "", None, None
    else:
        acc = default_bank_account(target, currency) if target else None
        if acc is None or acc.currency != currency:
            raise InstructionError(
                f"{target.code if target else 'Destination'} has no bank account in {currency}"
            )
        to_iban, bic, bank = acc.iban, acc.bic, acc.bank_name
        to_name = target.legal_name or target.name
    if not iban_is_valid(to_iban):
        raise InstructionError(f"destination IBAN {to_iban} is not valid")
    if not to_name:
        raise InstructionError("destination name is missing")
    instr = PaymentInstruction(
        from_company_id=payer.id,
        to_iban=to_iban,
        to_name=to_name,
        to_bic=bic,
        to_bank_name=bank,
        amount=amount,
        currency=currency,
        reference=reference,
        linked_rule_id=rule.id,
        status=InstructionStatus.PROPOSED,
        note=f"Created by payout rule #{rule.id}.",
    )
    session.add(instr)
    await session.flush()
    audit(
        session,
        "system",
        "instruction.proposed",
        f"instruction:{instr.id}",
        {"payer": payer.code, "to": to_name, "amount": str(amount), "rule": rule.id},
    )
    return instr


def needs_two_approvals(instr: PaymentInstruction, settings: Settings) -> bool:
    """Above MAX_PAYMENT_INSTRUCTION_AMOUNT a payment needs two different approvers."""
    return instr.amount > settings.max_payment_instruction_amount


async def approve(
    session: AsyncSession, settings: Settings, instr: PaymentInstruction, approver_tg: int
) -> bool:
    """True when the instruction is approved; False when only the first of two approvals
    needed above the limit was recorded."""
    if instr.status != InstructionStatus.PROPOSED:
        raise InstructionError(f"Instruction #{instr.id} is already {instr.status.value}")
    if needs_two_approvals(instr, settings):
        if instr.first_approved_by is None:
            instr.first_approved_by, instr.first_approved_at = approver_tg, datetime.now(UTC)
            audit(
                session,
                f"tg:{approver_tg}",
                "instruction.first_approval",
                f"instruction:{instr.id}",
                {"amount": str(instr.amount), "to": instr.to_name},
            )
            return False
        if instr.first_approved_by == approver_tg:
            raise InstructionError(
                "You already gave the first approval. A different approver must give the second."
            )
    instr.status = InstructionStatus.AWAITING_BANK
    instr.approved_by, instr.approved_at = approver_tg, datetime.now(UTC)
    audit(
        session,
        f"tg:{approver_tg}",
        "instruction.approved",
        f"instruction:{instr.id}",
        {"amount": str(instr.amount), "to": instr.to_name},
    )
    return True


async def cancel(session: AsyncSession, instr: PaymentInstruction, actor: str) -> None:
    if instr.status not in OPEN:
        raise InstructionError(f"Instruction #{instr.id} is already {instr.status.value}")
    instr.status = InstructionStatus.CANCELLED
    audit(session, actor, "instruction.cancelled", f"instruction:{instr.id}")


async def match_outgoing(session: AsyncSession, bt: BankTransfer) -> PaymentInstruction | None:
    """An outgoing bank transfer that executes an approved instruction (same payer, amount and
    currency, plus the beneficiary IBAN or the reference)."""
    if bt.direction != TransferDirection.OUTGOING or bt.company_id is None:
        return None
    pool = (
        await session.scalars(
            select(PaymentInstruction).where(
                PaymentInstruction.from_company_id == bt.company_id,
                PaymentInstruction.status.in_(
                    (InstructionStatus.APPROVED, InstructionStatus.AWAITING_BANK)
                ),
                PaymentInstruction.currency == bt.currency,
                PaymentInstruction.amount == bt.amount,
            )
        )
    ).all()
    benef, ref = normalize_iban(bt.beneficiary_account or ""), _norm_ref(bt.reference)

    def strong(i: PaymentInstruction) -> bool:
        by_iban = bool(benef) and benef == i.to_iban
        by_ref = bool(_norm_ref(i.reference)) and _norm_ref(i.reference) in ref
        return by_iban or by_ref

    hits = [i for i in pool if strong(i)]
    if len(hits) != 1:
        return None
    instr = hits[0]
    instr.status, instr.matched_transfer_id = InstructionStatus.EXECUTED, bt.id
    instr.executed_at = datetime.now(UTC)
    audit(
        session,
        "system",
        "instruction.executed",
        f"instruction:{instr.id}",
        {"transfer": bt.external_id, "amount": str(bt.amount)},
    )
    return instr
