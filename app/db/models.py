from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.db.types import Money


def utcnow() -> datetime:
    return datetime.now(UTC)


def _enum(e: type[StrEnum]) -> Enum:
    # Stored as plain strings (no native enum) so SQLite and Postgres behave the same.
    return Enum(e, native_enum=False, length=32, values_callable=lambda x: [m.value for m in x])


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON}


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# --- Enums -------------------------------------------------------------------


class InvoiceStatus(StrEnum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    SENT = "SENT"
    PAID = "PAID"
    CANCELLED = "CANCELLED"


class InvoiceSource(StrEnum):
    MANUAL = "manual"
    RULE = "rule"


class TransferDirection(StrEnum):
    INCOMING = "incoming"
    OUTGOING = "outgoing"
    INTERNAL = "internal"


class MatchConfidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class InstructionStatus(StrEnum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    AWAITING_BANK = "AWAITING_BANK"
    EXECUTED = "EXECUTED"
    CANCELLED = "CANCELLED"


class RuleType(StrEnum):
    INVOICE_ON_RECEIPT = "INVOICE_ON_RECEIPT"
    PAYOUT_ON_RECEIPT = "PAYOUT_ON_RECEIPT"


class UserRole(StrEnum):
    OPERATOR = "operator"
    APPROVER = "approver"


class FeeRuleType(StrEnum):
    PERCENT = "percent"
    FIXED = "fixed"


# --- Companies, templates, counterparties ------------------------------------


class Company(Timestamped, Base):
    """An issuer profile (X, Z, G...). Everything printed on an invoice comes from here."""

    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True)  # short handle used in commands
    name: Mapped[str] = mapped_column(String(255))
    legal_name: Mapped[str | None] = mapped_column(String(255))
    eik: Mapped[str | None] = mapped_column(String(32))
    vat_id: Mapped[str | None] = mapped_column(String(32))
    address: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(String(128))
    country: Mapped[str | None] = mapped_column(String(64))
    manager: Mapped[str | None] = mapped_column(String(255))  # representative / signatory
    phone: Mapped[str | None] = mapped_column(String(64))
    website: Mapped[str | None] = mapped_column(String(255))
    logo_path: Mapped[str | None] = mapped_column(String(512))

    email_from: Mapped[str | None] = mapped_column(String(255))
    mail_config_ref: Mapped[str | None] = mapped_column(String(64))  # key into SMTP/IMAP config

    invoice_prefix: Mapped[str] = mapped_column(String(16), default="")
    invoice_next_number: Mapped[int] = mapped_column(Integer, default=1)
    default_currency: Mapped[str] = mapped_column(String(3), default="EUR")
    default_vat_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("20.00"))
    no_vat_note: Mapped[str | None] = mapped_column(Text)  # legal note printed when VAT is 0
    default_payment_terms_days: Mapped[int] = mapped_column(Integer, default=14)

    default_template_id: Mapped[int | None] = mapped_column(
        ForeignKey("invoice_templates.id", use_alter=True, name="fk_company_default_template")
    )

    templates: Mapped[list["InvoiceTemplate"]] = relationship(
        back_populates="company", foreign_keys="InvoiceTemplate.company_id"
    )
    bank_accounts: Mapped[list["CompanyBankAccount"]] = relationship(
        back_populates="company", order_by="CompanyBankAccount.id"
    )


class CompanyBankAccount(Timestamped, Base):
    """One of a company's bank accounts. The invoice currency picks which one is printed."""

    __tablename__ = "company_bank_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    label: Mapped[str] = mapped_column(String(128))  # e.g. "EUR - Paynetics"
    currency: Mapped[str] = mapped_column(String(3))
    iban: Mapped[str] = mapped_column(String(64))
    bic: Mapped[str | None] = mapped_column(String(16))
    bank_name: Mapped[str | None] = mapped_column(String(255))
    # Paynetics account number, when this account is watched through the bank API.
    bank_account_ref: Mapped[str | None] = mapped_column(String(64))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)

    company: Mapped[Company] = relationship(back_populates="bank_accounts")


class NotePreset(Timestamped, Base):
    """Reusable invoice note / footer text, picked from a list instead of retyped."""

    __tablename__ = "note_presets"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))  # None = global
    title: Mapped[str] = mapped_column(String(128))
    text: Mapped[str] = mapped_column(Text)


class InvoiceTemplate(Timestamped, Base):
    """A per-company invoice design: a base layout plus style/content options.

    `layout` picks the Jinja2 base template file; `options` holds the variation knobs
    (accent colour, font, which blocks are shown, footer text...). Several templates
    per company are allowed; Company.default_template_id picks the default.
    """

    __tablename__ = "invoice_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    name: Mapped[str] = mapped_column(String(128))
    layout: Mapped[str] = mapped_column(String(64), default="classic")
    options: Mapped[dict[str, Any]] = mapped_column(default=dict)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)

    company: Mapped[Company] = relationship(back_populates="templates", foreign_keys=[company_id])

    __table_args__ = (UniqueConstraint("company_id", "name"),)


class DeliveryChannel(StrEnum):
    EMAIL = "email"
    TELEGRAM = "telegram"


class Counterparty(Timestamped, Base):
    """A client profile: what is printed as "Customer" and where invoices are delivered."""

    __tablename__ = "counterparties"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    eik: Mapped[str | None] = mapped_column(String(32))
    vat_id: Mapped[str | None] = mapped_column(String(32))
    address: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(String(255))
    iban: Mapped[str | None] = mapped_column(String(64))
    # Set when this counterparty is one of our own companies (X invoicing Z, etc.).
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    city: Mapped[str | None] = mapped_column(String(128))
    country: Mapped[str | None] = mapped_column(String(64))
    contact_name: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(64))
    # Telegram user or group chat id; the bot can only write there after the chat started it.
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    # Channels used to deliver approved invoices, e.g. ["email", "telegram"].
    delivery_channels: Mapped[list[str] | None] = mapped_column(JSON)
    notes: Mapped[str | None] = mapped_column(Text)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")


class Contract(Timestamped, Base):
    __tablename__ = "contracts"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))  # issuer
    counterparty_id: Mapped[int] = mapped_column(ForeignKey("counterparties.id"))
    service_description: Mapped[str] = mapped_column(Text)
    fee_rule: Mapped[FeeRuleType] = mapped_column(_enum(FeeRuleType), default=FeeRuleType.PERCENT)
    fee_value: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))
    payment_terms_days: Mapped[int] = mapped_column(Integer, default=14)
    template_id: Mapped[int | None] = mapped_column(ForeignKey("invoice_templates.id"))


# --- Invoices -----------------------------------------------------------------


class Invoice(Timestamped, Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str | None] = mapped_column(String(32))  # assigned on approval, no gaps
    issuer_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    counterparty_id: Mapped[int] = mapped_column(ForeignKey("counterparties.id"))
    template_id: Mapped[int | None] = mapped_column(ForeignKey("invoice_templates.id"))
    bank_account_id: Mapped[int | None] = mapped_column(ForeignKey("company_bank_accounts.id"))
    description: Mapped[str] = mapped_column(Text, default="")  # summary / basis of the invoice
    notes: Mapped[str | None] = mapped_column(Text)
    amount_net: Mapped[Decimal] = mapped_column(Money)
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    amount_vat: Mapped[Decimal] = mapped_column(Money)
    amount_gross: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3))
    issue_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[InvoiceStatus] = mapped_column(_enum(InvoiceStatus), default=InvoiceStatus.DRAFT)
    pdf_path: Mapped[str | None] = mapped_column(String(512))
    source: Mapped[InvoiceSource] = mapped_column(
        _enum(InvoiceSource), default=InvoiceSource.MANUAL
    )
    related_transfer_id: Mapped[int | None] = mapped_column(ForeignKey("bank_transfers.id"))
    send_idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    # Unguessable token for the public invoice link (/i/<token>), set on approval.
    public_token: Mapped[str | None] = mapped_column(String(64), index=True, unique=True)
    # Issuer/customer/bank data frozen at approval, so later profile edits never change it.
    snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_by: Mapped[int | None] = mapped_column(BigInteger)  # telegram id
    approved_by: Mapped[int | None] = mapped_column(BigInteger)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("issuer_id", "number"),)

    lines: Mapped[list["InvoiceLine"]] = relationship(
        back_populates="invoice", order_by="InvoiceLine.position", cascade="all, delete-orphan"
    )


class InvoiceDelivery(Base):
    """One attempt to deliver an invoice to a client over one channel."""

    __tablename__ = "invoice_deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"))
    channel: Mapped[str] = mapped_column(String(16))
    recipient: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16))  # sent | dry_run | failed | skipped
    detail: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    description: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("1"))
    unit: Mapped[str | None] = mapped_column(String(16))
    unit_price: Mapped[Decimal] = mapped_column(Money)
    amount: Mapped[Decimal] = mapped_column(Money)

    invoice: Mapped[Invoice] = relationship(back_populates="lines")


# --- Bank side ----------------------------------------------------------------


class BankTransfer(Timestamped, Base):
    """Cached copy of a transfer as seen at the bank."""

    __tablename__ = "bank_transfers"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(64))
    direction: Mapped[TransferDirection] = mapped_column(_enum(TransferDirection))
    status: Mapped[str] = mapped_column(String(32))
    amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3))
    payer_name: Mapped[str | None] = mapped_column(String(255))
    payer_account: Mapped[str | None] = mapped_column(String(64))
    beneficiary_name: Mapped[str | None] = mapped_column(String(255))
    beneficiary_account: Mapped[str | None] = mapped_column(String(64))
    reference: Mapped[str | None] = mapped_column(Text)
    booked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Which of our companies/accounts the transfer belongs to (resolved at sync time).
    # Plain column (no FK): adding a FK on SQLite would force a table rebuild in migration 3.
    company_id: Mapped[int | None] = mapped_column(Integer, index=True)
    account: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (UniqueConstraint("provider", "external_id"),)


class PaymentMatch(Timestamped, Base):
    """How an incoming transfer was tied to an invoice. One row per transfer; invoice_id is
    None while waiting for a human decision or when marked as not an invoice payment."""

    __tablename__ = "payment_matches"

    id: Mapped[int] = mapped_column(primary_key=True)
    transfer_id: Mapped[int] = mapped_column(
        ForeignKey("bank_transfers.id"), index=True, unique=True
    )
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"))
    confidence: Mapped[MatchConfidence] = mapped_column(_enum(MatchConfidence))
    method: Mapped[str] = mapped_column(String(64))
    confirmed_by_user: Mapped[int | None] = mapped_column(BigInteger)


class PaymentInstruction(Timestamped, Base):
    __tablename__ = "payment_instructions"

    id: Mapped[int] = mapped_column(primary_key=True)
    from_company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    to_iban: Mapped[str] = mapped_column(String(64))
    to_name: Mapped[str] = mapped_column(String(255))
    amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3))
    reference: Mapped[str] = mapped_column(String(255))
    linked_invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"))
    linked_rule_id: Mapped[int | None] = mapped_column(ForeignKey("rules.id"))
    status: Mapped[InstructionStatus] = mapped_column(
        _enum(InstructionStatus), default=InstructionStatus.PROPOSED
    )
    matched_transfer_id: Mapped[int | None] = mapped_column(ForeignKey("bank_transfers.id"))
    # From the supplier's invoice (Flow D); all optional so older rows stay valid.
    to_bic: Mapped[str | None] = mapped_column(String(16))
    to_bank_name: Mapped[str | None] = mapped_column(String(255))
    supplier_invoice_number: Mapped[str | None] = mapped_column(String(64))
    supplier_invoice_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    document_path: Mapped[str | None] = mapped_column(String(512))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    approved_by: Mapped[int | None] = mapped_column(BigInteger)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)


class Rule(Timestamped, Base):
    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[RuleType] = mapped_column(_enum(RuleType))
    trigger_company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    target_company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class RuleRun(Base):
    """One evaluation of a rule for one bank transfer; makes rules fire at most once."""

    __tablename__ = "rule_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("rules.id"))
    transfer_id: Mapped[int] = mapped_column(ForeignKey("bank_transfers.id"))
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"))
    status: Mapped[str] = mapped_column(String(16))  # drafted | skipped | failed
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("rule_id", "transfer_id"),)


# --- Access & audit -----------------------------------------------------------


class AllowedUser(Timestamped, Base):
    __tablename__ = "allowed_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    role: Mapped[UserRole] = mapped_column(_enum(UserRole), default=UserRole.OPERATOR)
    # Company codes this user may act for; empty list means all companies.
    companies: Mapped[list[str]] = mapped_column(JSON, default=list)
    display_name: Mapped[str | None] = mapped_column(String(255))


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor: Mapped[str] = mapped_column(String(64))  # "tg:<id>" or "system"
    action: Mapped[str] = mapped_column(String(64))
    entity: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)


class MockBankTransfer(Base):
    """Backing store for MockBankProvider; mirrors the Paynetics transfer object."""

    __tablename__ = "mock_bank_transfers"

    id: Mapped[int] = mapped_column(primary_key=True)
    transfer_id: Mapped[str] = mapped_column(String(32), unique=True)
    transfer_type: Mapped[TransferDirection] = mapped_column(_enum(TransferDirection))
    account: Mapped[str] = mapped_column(String(64))  # our account the transfer belongs to
    payer: Mapped[str] = mapped_column(String(255))
    payer_account: Mapped[str | None] = mapped_column(String(64))
    beneficiary: Mapped[str] = mapped_column(String(255))
    beneficiary_account: Mapped[str | None] = mapped_column(String(64))
    amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3))
    payment_details: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="processed")
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
