"""Plain data handed to invoice layouts. Built from DB rows or from sample data (preview)."""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from app.db.models import Company, CompanyBankAccount
from app.money import CENT, to_money


@dataclass
class Party:
    name: str
    address: str = ""
    city: str = ""
    country: str = ""
    eik: str = ""
    vat_id: str = ""
    manager: str = ""
    email: str = ""
    phone: str = ""
    website: str = ""


@dataclass
class Line:
    description: str
    quantity: Decimal
    unit_price: Decimal
    unit: str = ""

    @property
    def amount(self) -> Decimal:
        return (self.quantity * self.unit_price).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class BankDetails:
    iban: str
    bic: str = ""
    bank_name: str = ""


@dataclass
class InvoiceView:
    number: str
    issue_date: date
    due_date: date | None
    supplier: Party
    customer: Party
    lines: list[Line]
    currency: str
    vat_rate: Decimal
    bank: BankDetails | None = None
    description: str = ""
    notes: str = ""
    no_vat_note: str = ""
    logo_url: str | None = None
    is_draft: bool = False
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def net(self) -> Decimal:
        return to_money(sum((line.amount for line in self.lines), Decimal(0)))

    @property
    def vat(self) -> Decimal:
        return to_money(self.net * self.vat_rate / 100)

    @property
    def gross(self) -> Decimal:
        return self.net + self.vat


def party_from_company(c: Company) -> Party:
    return Party(
        name=c.legal_name or c.name,
        address=c.address or "",
        city=c.city or "",
        country=c.country or "",
        eik=c.eik or "",
        vat_id=c.vat_id or "",
        manager=c.manager or "",
        email=c.email_from or "",
        phone=c.phone or "",
        website=c.website or "",
    )


def bank_from_account(a: CompanyBankAccount | None) -> BankDetails | None:
    if a is None:
        return None
    return BankDetails(iban=a.iban, bic=a.bic or "", bank_name=a.bank_name or "")


def sample_invoice(
    company: Company, bank: CompanyBankAccount | None, logo_url: str | None
) -> InvoiceView:
    """Realistic dummy invoice for template previews."""
    today = date.today()
    prefix = company.invoice_prefix or ""
    return InvoiceView(
        number=f"{prefix}{company.invoice_next_number:010d}",
        issue_date=today,
        due_date=today + timedelta(days=company.default_payment_terms_days),
        supplier=party_from_company(company),
        customer=Party(
            name="Sample Client Ltd",
            address="10 Customer Avenue",
            city="Plovdiv",
            country="Bulgaria",
            eik="123456789",
            vat_id="BG123456789",
            manager="Alex Client",
        ),
        lines=[
            Line("Consulting services - October 2026", Decimal("1"), Decimal("1200.00")),
            Line("Platform subscription", Decimal("3"), Decimal("49.90"), "month"),
        ],
        currency=(bank.currency if bank else company.default_currency),
        vat_rate=company.default_vat_rate,
        bank=bank_from_account(bank),
        description="Services under contract No. 12/2026",
        notes="Thank you for your business.",
        no_vat_note=company.no_vat_note or "",
        logo_url=logo_url,
        is_draft=False,
    )
