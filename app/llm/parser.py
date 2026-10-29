"""Free text -> InvoiceDraft via Claude structured output.

The model only proposes a draft; nothing here has side effects. The caller resolves the
proposed issuer/client against the DB and a human approves everything in Telegram.
"""

import base64
import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from app.money import InvalidAmount, to_money

log = logging.getLogger(__name__)


class ParserUnavailable(RuntimeError):
    """No API key configured."""


class ParserError(RuntimeError):
    """The model could not produce a valid draft (after one retry) or the API failed."""


class DraftLine(BaseModel):
    _blanks = model_validator(mode="before")(classmethod(lambda cls, d: _blank_to_none(d)))

    # Empty when the operator gave none ("invoice for 36k"): the bot fills a default and warns.
    description: str = Field(default="", max_length=500)
    quantity: Decimal = Decimal("1")
    unit_price: Decimal
    unit: str | None = None

    @field_validator("description", mode="before")
    @classmethod
    def _desc(cls, v: Any) -> str:
        return str(v or "").strip()[:500]

    @field_validator("quantity", mode="before")
    @classmethod
    def _qty(cls, v: Any) -> Decimal:
        m = re.search(r"-?\d+(?:[.,]\d+)?", str(v or ""))
        if not m:
            return Decimal("1")  # "", "one", missing: one unit
        d = Decimal(m.group(0).replace(",", "."))
        if d <= 0:
            raise ValueError("quantity must be positive")
        return d

    @field_validator("unit_price", mode="before")
    @classmethod
    def _price(cls, v: Any) -> Decimal:
        try:
            d = to_money(str(v))
        except InvalidAmount as e:
            raise ValueError(str(e)) from e
        if d <= 0:
            raise ValueError("unit price must be positive")
        return d


class PartyDetails(BaseModel):
    """Company details found in the message or attached documents (all optional)."""

    name: str | None = None  # full legal name
    company_number: str | None = None  # registration number / EIK / Companies House number
    vat_id: str | None = None
    address: str | None = None  # street address
    city: str | None = None
    country: str | None = None
    email: str | None = None
    contact_name: str | None = None

    _blanks = model_validator(mode="before")(classmethod(lambda cls, d: _blank_to_none(d)))

    def filled(self) -> dict[str, str]:
        return {
            k: v.strip() for k, v in self.model_dump().items() if isinstance(v, str) and v.strip()
        }


class DocumentRead(BaseModel):
    """Both sides and the payment facts of an invoice (or a company details sheet)."""

    _blanks = model_validator(mode="before")(classmethod(lambda cls, d: _blank_to_none(d)))

    doc_type: Literal["invoice", "company_details", "other"]
    issuer: PartyDetails = Field(default_factory=PartyDetails)  # supplier / company described
    recipient: PartyDetails = Field(default_factory=PartyDetails)  # bill-to / customer
    issuer_iban: str | None = None
    issuer_bic: str | None = None
    issuer_bank_name: str | None = None
    invoice_number: str | None = None
    issue_date: date | None = None
    due_date: date | None = None
    currency: str | None = None
    total: Decimal | None = None  # amount due incl. VAT
    payment_reference: str | None = None

    @field_validator("issue_date", "due_date", mode="before")
    @classmethod
    def _date(cls, v: Any) -> Any:
        if isinstance(v, str):
            try:
                return date.fromisoformat(v.strip())
            except ValueError:
                return None
        return v

    @field_validator("total", mode="before")
    @classmethod
    def _total(cls, v: Any) -> Decimal | None:
        if v in (None, ""):
            return None
        try:
            return to_money(str(v).replace(" ", ""))
        except InvalidAmount:
            return None

    @field_validator("currency")
    @classmethod
    def _cur(cls, v: str | None) -> str | None:
        v = (v or "").strip().upper()
        return v if len(v) == 3 and v.isalpha() else None


_CURRENCY_WORDS = {
    "€": "EUR",
    "EURO": "EUR",
    "EUROS": "EUR",
    "$": "USD",
    "DOLLAR": "USD",
    "DOLLARS": "USD",
    "£": "GBP",
    "POUND": "GBP",
    "POUNDS": "GBP",
    "ЛВ": "BGN",
    "ЛВ.": "BGN",
    "LEVA": "BGN",
}


class InvoiceDraft(BaseModel):
    _blanks = model_validator(mode="before")(classmethod(lambda cls, d: _blank_to_none(d)))

    # invoice: issue one | pay: a supplier invoice we must pay | client: save the client's
    # details | company: create/update one of OUR company profiles | other
    intent: Literal["invoice", "pay", "client", "company", "other"]
    issuer_code: str | None = None  # one of our company codes, or None if unclear
    issuer_text: str | None = None  # issuer as the user wrote it
    client_id: int | None = None  # one of the known client ids, or None
    client_company_code: str | None = None  # when the client is one of OUR companies
    client_text: str | None = None  # client as the user wrote it
    client_details: PartyDetails | None = None
    currency: str | None = None
    vat_rate: Decimal | None = None
    amounts_include_vat: bool = False
    lines: list[DraftLine] = Field(default_factory=list)
    due_in_days: int | None = Field(default=None, ge=0, le=365)
    notes: str | None = None
    unclear: str | None = None  # short note on anything ambiguous

    # Every optional field degrades to None instead of failing the whole request: the bot then
    # asks for what is missing (one odd value from the model must not lose the message).
    @field_validator("currency", mode="before")
    @classmethod
    def _cur(cls, v: Any) -> str | None:
        if v is None:
            return None
        v = str(v).strip().upper()
        v = _CURRENCY_WORDS.get(v, v)
        return v if len(v) == 3 and v.isalpha() else None

    @field_validator("vat_rate", mode="before")
    @classmethod
    def _vat(cls, v: Any) -> Decimal | None:
        m = re.search(r"\d+(?:[.,]\d+)?", str(v or ""))
        if not m:
            return None
        d = Decimal(m.group(0).replace(",", "."))
        return d if 0 <= d <= 100 else None

    @field_validator("lines", mode="before")
    @classmethod
    def _lines(cls, v: Any) -> list[Any]:
        """Drop lines without a usable price instead of rejecting the whole draft; with no lines
        left the bot asks for the amount."""
        out = []
        for item in v or []:
            try:
                out.append(DraftLine.model_validate(item))
            except ValidationError:
                log.info("dropped a draft line without a usable price: %.200s", item)
        return out

    @field_validator("client_id", mode="before")
    @classmethod
    def _client_id(cls, v: Any) -> int | None:
        s = str(v or "").strip()
        return int(s) if s.isdigit() else None

    @field_validator("due_in_days", mode="before")
    @classmethod
    def _due(cls, v: Any) -> int | None:
        m = re.search(r"\d+", str(v or ""))
        return int(m.group(0)) if m and int(m.group(0)) <= 365 else None


# Structured outputs cap the number of union-typed (nullable) fields, so every optional value is
# a plain string and "" means "not provided"; _blank_to_none turns it back into None.
_STR: dict[str, Any] = {"type": "string"}


def _blank_to_none(data: Any) -> Any:
    if isinstance(data, dict):
        return {k: (None if isinstance(v, str) and not v.strip() else v) for k, v in data.items()}
    return data


_PARTY_KEYS = [
    "name",
    "company_number",
    "vat_id",
    "address",
    "city",
    "country",
    "email",
    "contact_name",
]
PARTY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {k: _STR for k in _PARTY_KEYS},
    "required": _PARTY_KEYS,
    "additionalProperties": False,
}

_DOC_KEYS = [
    "doc_type",
    "issuer",
    "recipient",
    "issuer_iban",
    "issuer_bic",
    "issuer_bank_name",
    "invoice_number",
    "issue_date",
    "due_date",
    "currency",
    "total",
    "payment_reference",
]
DOC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "doc_type": {"type": "string", "enum": ["invoice", "company_details", "other"]},
        "issuer": PARTY_SCHEMA,
        "recipient": PARTY_SCHEMA,
        **{k: _STR for k in _DOC_KEYS[3:]},
        "issuer_iban": _STR,
    },
    "required": _DOC_KEYS,
    "additionalProperties": False,
}

# Amounts travel as strings so they never pass through a float.
DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["invoice", "pay", "client", "company", "other"]},
        "issuer_code": _STR,
        "issuer_text": _STR,
        "client_id": _STR,
        "client_company_code": _STR,
        "client_text": _STR,
        "client_details": PARTY_SCHEMA,
        "currency": _STR,
        "vat_rate": _STR,
        "amounts_include_vat": {"type": "boolean"},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "quantity": {"type": "string"},
                    "unit_price": {"type": "string"},
                    "unit": _STR,
                },
                "required": ["description", "quantity", "unit_price", "unit"],
                "additionalProperties": False,
            },
        },
        "due_in_days": _STR,
        "notes": _STR,
        "unclear": _STR,
    },
    "required": [
        "intent",
        "issuer_code",
        "issuer_text",
        "client_id",
        "client_text",
        "currency",
        "vat_rate",
        "amounts_include_vat",
        "lines",
        "due_in_days",
        "notes",
        "unclear",
    ],
    "additionalProperties": False,
}

SYSTEM = """You turn an operator's message (and any attached images or PDFs, e.g. another \
invoice, a company card, a screenshot) into a structured invoice draft for an internal invoicing \
tool. You never take actions; a human reviews every draft.

Rules:
- intent: "invoice" when the operator asks to issue/create/prepare an invoice, or sends a \
document to invoice from (or, when a current draft is given, asks to change it); "pay" when \
they send/describe an invoice we RECEIVED and must pay; "client" when they only want to save a \
client's details; "company" when they want to create/update one of OUR company profiles; \
otherwise "other". For anything but "invoice", leave the other fields empty.
- issuer_code: the code of OUR company that issues the invoice, chosen only from "Our companies". \
Match codes and names loosely (case, abbreviations, missing "Ltd"). If unclear, "" and copy the \
wording to issuer_text.
- The client may be a known client (client_id from "Known clients") or one of OUR companies \
(client_company_code from "Our companies", e.g. "NSOL" or "Nove Solida" -> that code). Match \
loosely but only when it clearly refers to that entry; otherwise leave both "".
- client_text: the client as the operator wrote it (or as named in the document).
- client_details: every detail about the CLIENT that appears in the message or documents \
(legal name, company/registration number, VAT number, street address, city, country, email, \
contact person). Copy values exactly; never guess or invent. "" for anything not present.
- lines: one entry per billed item. unit_price is the price of ONE unit as a plain decimal \
string with a dot ("1200.00"); quantity is a decimal string ("1", "3"). "98k" means 98000. \
Never invent amounts: if no amount is given, return no lines and explain in unclear.
- amounts_include_vat: true only when it says the amount includes VAT ("incl. VAT", "gross", \
"with VAT"). Otherwise false (amounts are net).
- currency: ISO code if stated or shown (EUR, USD, GBP, BGN...), else "". vat_rate: percentage \
as a string only if stated ("0", "9", "20"), else "". due_in_days: only if stated.
- notes: any extra text the operator wants printed on the invoice, else "".
- unclear: one short sentence about anything ambiguous or missing, else empty.
- Every text field is a string: use "" (empty string) for anything unknown or not applicable, \
never the word "null". client_id and due_in_days are digits as a string, or "".
- When a current draft is provided, apply the requested change to it and return the full \
updated draft (keep everything not mentioned)."""

DOC_SYSTEM = """You read an attached business document for an internal invoicing tool. Copy \
values exactly as printed; never guess; use "" for anything not present.
- doc_type: "invoice" for an invoice/bill/pro-forma, "company_details" for a company card, \
registration extract or letterhead with company data, else "other".
- issuer: the company that ISSUED the invoice (the supplier, "From"); for company_details, the \
company described. recipient: the "Bill to"/customer company (empty for company_details).
- company_number is the registration number (EIK, Companies House number, etc.), not the VAT \
number. Split addresses into street address, city and country.
- issuer_iban / issuer_bic / issuer_bank_name: the bank details printed for paying the issuer.
- total: the final amount due including VAT, as a plain decimal string ("76908.00").
- issue_date / due_date: YYYY-MM-DD. currency: ISO code (EUR, USD, GBP...).
- payment_reference: the reference the payer is asked to use, if printed (else "")."""

PARTY_SYSTEM = """You extract a company's details from an operator's reply and any attached \
images or PDFs. Return only what is actually present; copy values exactly; never guess; \
use "" for anything not present. company_number is the registration number (EIK, \
Companies House number, etc.), not the VAT number. Split a full address into street \
address, city and country when possible."""


@dataclass(frozen=True)
class Attachment:
    media_type: str  # image/jpeg, image/png, image/webp, image/gif or application/pdf
    data: bytes

    def block(self) -> dict[str, Any]:
        b64 = base64.standard_b64encode(self.data).decode()
        kind = "document" if self.media_type == "application/pdf" else "image"
        return {
            "type": kind,
            "source": {"type": "base64", "media_type": self.media_type, "data": b64},
        }


SUPPORTED_MEDIA = {"image/jpeg", "image/png", "image/webp", "image/gif", "application/pdf"}


@dataclass(frozen=True)
class ParseContext:
    companies: list[tuple[str, str]]  # (code, name)
    clients: list[tuple[int, str]]  # (id, name)
    today: date
    current_draft: dict[str, Any] | None = None


def _user_prompt(text: str, ctx: ParseContext) -> str:
    companies = "\n".join(f"- {code}: {name}" for code, name in ctx.companies) or "- (none)"
    clients = "\n".join(f"- {cid}: {name}" for cid, name in ctx.clients) or "- (none)"
    parts = [
        f"Today: {ctx.today.isoformat()}",
        f"Our companies (code: name):\n{companies}",
        f"Known clients (id: name):\n{clients}",
    ]
    if ctx.current_draft is not None:
        parts.append(f"Current draft:\n{json.dumps(ctx.current_draft, default=str)}")
        parts.append(f"Requested change:\n{text or '(see attachments)'}")
    else:
        parts.append(f"Operator message:\n{text or '(no text; see attachments)'}")
    return "\n\n".join(parts)


def _short_errors(e: Exception | None) -> str:
    if not isinstance(e, ValidationError):
        return "no valid answer"
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()[:3]
    )[:200]


class InvoiceParser:
    def __init__(self, api_key: str | None, model: str) -> None:
        self.model = model
        self._client = (
            anthropic.AsyncAnthropic(api_key=api_key, timeout=90.0, max_retries=2)
            if api_key
            else None
        )

    @property
    def available(self) -> bool:
        return self._client is not None

    async def _call(
        self,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        attachments: tuple[Attachment, ...] = (),
    ) -> str:
        assert self._client is not None
        content: list[dict[str, Any]] = [a.block() for a in attachments]
        content.append({"type": "text", "text": prompt})
        try:
            response = await self._client.beta.messages.create(
                model=self.model,
                max_tokens=8000,
                system=system,
                messages=[{"role": "user", "content": content}],
                output_config={
                    # Reading documents needs more care than a one-line request.
                    "effort": "medium" if attachments else "low",
                    "format": {"type": "json_schema", "schema": schema},
                },
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as e:
            raise ParserError("Claude API key is invalid") from e
        except anthropic.RateLimitError as e:
            raise ParserError("Claude API rate limit reached, try again in a minute") from e
        except anthropic.BadRequestError as e:
            raise ParserError(f"Claude could not read this input: {e.message}"[:300]) from e
        except anthropic.APIStatusError as e:
            raise ParserError(f"Claude API error {e.status_code}") from e
        except anthropic.APIConnectionError as e:
            raise ParserError("Could not reach the Claude API") from e
        if response.stop_reason == "refusal":
            raise ParserError("The request was declined by the model")
        if response.stop_reason == "max_tokens":
            raise ParserError("The draft was too long to parse")
        return next((b.text for b in response.content if b.type == "text"), "")

    async def parse(
        self, text: str, ctx: ParseContext, attachments: tuple[Attachment, ...] = ()
    ) -> InvoiceDraft:
        if self._client is None:
            raise ParserUnavailable("ANTHROPIC_API_KEY is not set")
        prompt = _user_prompt(text, ctx)
        last_error: Exception | None = None
        for attempt in (1, 2):  # one retry on invalid output, told what was wrong
            raw = await self._call(SYSTEM, prompt, DRAFT_SCHEMA, attachments)
            try:
                return InvoiceDraft.model_validate_json(_strip_fences(raw))
            except ValidationError as e:
                last_error = e
                log.warning("invalid draft from model (attempt %d): %s | %.500s", attempt, e, raw)
                prompt = (
                    f"{_user_prompt(text, ctx)}\n\nYour previous answer was rejected: "
                    f"{_short_errors(e)}. Answer again following the field rules."
                )
        raise ParserError(
            f"Could not understand the request ({_short_errors(last_error)}); please rephrase it"
        ) from last_error

    async def read_document(self, text: str, attachments: tuple[Attachment, ...]) -> DocumentRead:
        if self._client is None:
            raise ParserUnavailable("ANTHROPIC_API_KEY is not set")
        prompt = f"Operator note: {text or '(none)'}"
        last_error: Exception | None = None
        for _ in (1, 2):
            raw = await self._call(DOC_SYSTEM, prompt, DOC_SCHEMA, attachments)
            try:
                return DocumentRead.model_validate_json(_strip_fences(raw))
            except ValidationError as e:
                last_error = e
        raise ParserError(
            "Could not read that document; try a clearer photo or the PDF"
        ) from last_error

    async def extract_party(
        self,
        text: str,
        known: dict[str, str],
        wanted: list[str],
        attachments: tuple[Attachment, ...] = (),
    ) -> PartyDetails:
        """Company details from a reply like "company no 12345, 1 Main St, London, UK"."""
        if self._client is None:
            raise ParserUnavailable("ANTHROPIC_API_KEY is not set")
        prompt = (
            f"Company we are completing: {json.dumps(known)}\n"
            f"Still missing: {', '.join(wanted)}\n\n"
            f"Operator reply:\n{text or '(no text; see attachments)'}"
        )
        last_error: Exception | None = None
        for _ in (1, 2):
            raw = await self._call(PARTY_SYSTEM, prompt, PARTY_SCHEMA, attachments)
            try:
                return PartyDetails.model_validate_json(_strip_fences(raw))
            except ValidationError as e:
                last_error = e
        raise ParserError("Could not read those details; please send them again") from last_error


def _strip_fences(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        s = s.rsplit("```", 1)[0]
    return s.strip()
