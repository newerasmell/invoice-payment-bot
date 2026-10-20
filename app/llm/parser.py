"""Free text -> InvoiceDraft via Claude structured output.

The model only proposes a draft; nothing here has side effects. The caller resolves the
proposed issuer/client against the DB and a human approves everything in Telegram.
"""

import json
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.money import InvalidAmount, to_money

log = logging.getLogger(__name__)


class ParserUnavailable(RuntimeError):
    """No API key configured."""


class ParserError(RuntimeError):
    """The model could not produce a valid draft (after one retry) or the API failed."""


class DraftLine(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    quantity: Decimal = Decimal("1")
    unit_price: Decimal
    unit: str | None = None

    @field_validator("quantity", mode="before")
    @classmethod
    def _qty(cls, v: Any) -> Decimal:
        d = Decimal(str(v).replace(",", "."))
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


class InvoiceDraft(BaseModel):
    intent: Literal["invoice", "other"]
    issuer_code: str | None = None  # one of our company codes, or None if unclear
    issuer_text: str | None = None  # issuer as the user wrote it
    client_id: int | None = None  # one of the known client ids, or None
    client_text: str | None = None  # client as the user wrote it
    currency: str | None = None
    vat_rate: Decimal | None = None
    amounts_include_vat: bool = False
    lines: list[DraftLine] = Field(default_factory=list)
    due_in_days: int | None = Field(default=None, ge=0, le=365)
    notes: str | None = None
    unclear: str | None = None  # short note on anything ambiguous

    @field_validator("currency")
    @classmethod
    def _cur(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().upper()
        if len(v) != 3 or not v.isalpha():
            raise ValueError("currency must be a 3-letter ISO code")
        return v

    @field_validator("vat_rate", mode="before")
    @classmethod
    def _vat(cls, v: Any) -> Decimal | None:
        if v is None or v == "":
            return None
        d = Decimal(str(v).replace(",", "."))
        if not (0 <= d <= 100):
            raise ValueError("vat_rate must be a percentage between 0 and 100")
        return d


def _nullable(t: str) -> dict[str, Any]:
    return {"anyOf": [{"type": t}, {"type": "null"}]}


# Amounts travel as strings so they never pass through a float.
DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["invoice", "other"]},
        "issuer_code": _nullable("string"),
        "issuer_text": _nullable("string"),
        "client_id": _nullable("integer"),
        "client_text": _nullable("string"),
        "currency": _nullable("string"),
        "vat_rate": _nullable("string"),
        "amounts_include_vat": {"type": "boolean"},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "quantity": {"type": "string"},
                    "unit_price": {"type": "string"},
                    "unit": _nullable("string"),
                },
                "required": ["description", "quantity", "unit_price", "unit"],
                "additionalProperties": False,
            },
        },
        "due_in_days": _nullable("integer"),
        "notes": _nullable("string"),
        "unclear": _nullable("string"),
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

SYSTEM = """You turn an operator's message into a structured invoice draft for an internal \
invoicing tool. You never take actions; a human reviews every draft.

Rules:
- intent is "invoice" only when the message asks to issue/create/prepare an invoice (or, when a \
current draft is given, asks to change it). Otherwise "other" and leave the rest empty.
- issuer_code: the code of OUR company that issues the invoice, chosen only from the list given. \
If the message does not clearly name one of them, use null and copy the wording to issuer_text.
- client_id: the id of the client, chosen only from the client list given. If the client is not \
in the list or it is ambiguous, use null and put the client name as written in client_text.
- lines: one entry per billed item. unit_price is the price of ONE unit as a plain decimal \
string with a dot ("1200.00"); quantity is a decimal string ("1", "3"). Never invent amounts: \
if no amount is given, return no lines and explain in unclear.
- amounts_include_vat: true only when the message says the amount includes VAT ("incl. VAT", \
"gross", "with VAT"). Otherwise false (amounts are net).
- currency: ISO code if stated (EUR, USD, BGN...), else null. vat_rate: percentage as a string \
only if stated ("0", "9", "20"), else null. due_in_days: only if stated ("due in 30 days").
- notes: any extra text the operator wants printed on the invoice, else null.
- unclear: one short sentence about anything ambiguous or missing, else null.
- When a current draft is provided, apply the requested change to it and return the full \
updated draft (keep everything not mentioned)."""


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
        parts.append(f"Requested change:\n{text}")
    else:
        parts.append(f"Operator message:\n{text}")
    return "\n\n".join(parts)


class InvoiceParser:
    def __init__(self, api_key: str | None, model: str) -> None:
        self.model = model
        self._client = (
            anthropic.AsyncAnthropic(api_key=api_key, timeout=60.0, max_retries=2)
            if api_key
            else None
        )

    @property
    def available(self) -> bool:
        return self._client is not None

    async def _call(self, prompt: str) -> str:
        assert self._client is not None
        try:
            response = await self._client.beta.messages.create(
                model=self.model,
                max_tokens=4000,
                system=SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": DRAFT_SCHEMA},
                },
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as e:
            raise ParserError("Claude API key is invalid") from e
        except anthropic.RateLimitError as e:
            raise ParserError("Claude API rate limit reached, try again in a minute") from e
        except anthropic.APIStatusError as e:
            raise ParserError(f"Claude API error {e.status_code}") from e
        except anthropic.APIConnectionError as e:
            raise ParserError("Could not reach the Claude API") from e
        if response.stop_reason == "refusal":
            raise ParserError("The request was declined by the model")
        if response.stop_reason == "max_tokens":
            raise ParserError("The draft was too long to parse")
        text = next((b.text for b in response.content if b.type == "text"), "")
        return text

    async def parse(self, text: str, ctx: ParseContext) -> InvoiceDraft:
        if self._client is None:
            raise ParserUnavailable("ANTHROPIC_API_KEY is not set")
        prompt = _user_prompt(text, ctx)
        last_error: Exception | None = None
        for attempt in (1, 2):  # one retry on invalid output
            raw = await self._call(prompt)
            try:
                return InvoiceDraft.model_validate_json(_strip_fences(raw))
            except ValidationError as e:
                last_error = e
                log.warning("invalid draft from model (attempt %d): %s", attempt, e)
        raise ParserError("Could not understand the request; please rephrase it") from last_error


def _strip_fences(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        s = s.rsplit("```", 1)[0]
    return s.strip()
