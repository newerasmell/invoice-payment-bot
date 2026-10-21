"""What must be known about the issuer and the client before an invoice can exist.

The bot collects missing client data in the chat; missing issuer data has to be completed in
the admin panel (company profile and bank accounts). Approval re-checks everything.
"""

from app.companies import default_bank_account
from app.db.models import Company, Counterparty
from app.llm.parser import PartyDetails

# (Counterparty attribute, label shown to the operator)
CLIENT_REQUIRED: list[tuple[str, str]] = [
    ("name", "full legal name"),
    ("eik", "company number"),
    ("address", "street address"),
    ("city", "city"),
    ("country", "country"),
]
ISSUER_REQUIRED: list[tuple[str, str]] = [
    ("eik", "company number"),
    ("address", "street address"),
    ("city", "city"),
    ("country", "country"),
]
# PartyDetails field -> Counterparty attribute
_PARTY_TO_CLIENT = {
    "name": "name",
    "company_number": "eik",
    "vat_id": "vat_id",
    "address": "address",
    "city": "city",
    "country": "country",
    "email": "email",
    "contact_name": "contact_name",
}
LABELS = dict(CLIENT_REQUIRED) | {
    "vat_id": "VAT number",
    "email": "email",
    "contact_name": "contact person",
}


def _blank(v: object) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def missing_client(c: Counterparty, confirm_name: bool = False) -> list[str]:
    """Attributes still missing. `confirm_name` asks for the legal name even if one is set
    (a client created from a short name like "NSOL")."""
    out = [attr for attr, _ in CLIENT_REQUIRED if _blank(getattr(c, attr))]
    if confirm_name and "name" not in out:
        out.insert(0, "name")
    return out


def missing_issuer(company: Company, currency: str) -> list[str]:
    """Human-readable list of what the issuing company's profile lacks."""
    out = []
    if _blank(company.legal_name) and _blank(company.name):
        out.append("legal name")
    out += [label for attr, label in ISSUER_REQUIRED if _blank(getattr(company, attr))]
    if default_bank_account(company, currency) is None or not any(
        a.currency == currency for a in company.bank_accounts
    ):
        out.append(f"bank account in {currency}")
    return out


def apply_details(
    c: Counterparty, details: PartyDetails | None, *, overwrite: set[str] | None = None
) -> list[str]:
    """Fill the client's empty fields from extracted details (and `overwrite` ones even if
    set). Returns the attributes that changed."""
    if details is None:
        return []
    overwrite = overwrite or set()
    changed = []
    for src, value in details.filled().items():
        attr = _PARTY_TO_CLIENT[src]
        if (_blank(getattr(c, attr)) or attr in overwrite) and getattr(c, attr) != value:
            setattr(c, attr, value)
            changed.append(attr)
    return changed


def labels(attrs: list[str]) -> str:
    return ", ".join(LABELS.get(a, a) for a in attrs)
