import json
from datetime import date
from decimal import Decimal

import pytest

from app.llm.parser import (
    DRAFT_SCHEMA,
    InvoiceDraft,
    InvoiceParser,
    ParseContext,
    ParserError,
    ParserUnavailable,
    _strip_fences,
    _user_prompt,
)

CTX = ParseContext(
    companies=[("X", "Company X Ltd"), ("Z", "Company Z Ltd")],
    clients=[(7, "Client Ltd")],
    today=date(2026, 10, 20),
)

GOOD = {
    "intent": "invoice",
    "issuer_code": "Z",
    "issuer_text": "Company Z",
    "client_id": 7,
    "client_text": "Client Ltd",
    "currency": "eur",
    "vat_rate": "20",
    "amounts_include_vat": False,
    "lines": [
        {"description": "Consulting", "quantity": "1", "unit_price": "1200.00", "unit": None}
    ],
    "due_in_days": None,
    "notes": None,
    "unclear": None,
}


def parser_returning(*outputs: str) -> InvoiceParser:
    p = InvoiceParser("test-key", "claude-sonnet-5-5")
    queue = list(outputs)

    async def fake_call(prompt: str) -> str:
        return queue.pop(0)

    p._call = fake_call  # type: ignore[method-assign]
    return p


async def test_valid_output() -> None:
    d = await parser_returning(json.dumps(GOOD)).parse("Invoice Client Ltd 1200 EUR from Z", CTX)
    assert d.issuer_code == "Z" and d.client_id == 7 and d.currency == "EUR"
    assert d.lines[0].unit_price == Decimal("1200.00") and d.vat_rate == Decimal("20")


async def test_retries_once_then_fails() -> None:
    bad = json.dumps({**GOOD, "lines": [{**GOOD["lines"][0], "unit_price": "abc"}]})
    d = await parser_returning(bad, json.dumps(GOOD)).parse("x", CTX)
    assert d.lines[0].unit_price == Decimal("1200.00")
    with pytest.raises(ParserError, match="rephrase"):
        await parser_returning("not json", bad).parse("x", CTX)


@pytest.mark.parametrize(
    ("patch", "msg"),
    [
        ({"currency": "EURO"}, "currency"),
        ({"vat_rate": "150"}, "vat_rate"),
        (
            {"lines": [{"description": "x", "quantity": "-1", "unit_price": "5", "unit": None}]},
            "quantity",
        ),
        (
            {"lines": [{"description": "x", "quantity": "1", "unit_price": "0", "unit": None}]},
            "unit price",
        ),
        ({"due_in_days": 9999}, "due_in_days"),
    ],
)
def test_validation_rejects_bad_values(patch: dict, msg: str) -> None:
    with pytest.raises(ValueError, match=msg):
        InvoiceDraft.model_validate({**GOOD, **patch})


def test_no_floats_in_money() -> None:
    d = InvoiceDraft.model_validate(
        {
            **GOOD,
            "lines": [{"description": "x", "quantity": "3", "unit_price": "0.1", "unit": None}],
        }
    )
    assert d.lines[0].unit_price == Decimal("0.10") and isinstance(d.lines[0].quantity, Decimal)
    line_schema = DRAFT_SCHEMA["properties"]["lines"]["items"]["properties"]
    assert line_schema["unit_price"] == {"type": "string"}


def test_strip_fences_and_prompt() -> None:
    assert _strip_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
    prompt = _user_prompt(
        "make it 1500", ParseContext(**{**CTX.__dict__, "current_draft": {"lines": []}})
    )
    assert "Current draft" in prompt and "Requested change:\nmake it 1500" in prompt
    assert "- X: Company X Ltd" in prompt and "- 7: Client Ltd" in prompt


async def test_unavailable_without_key() -> None:
    p = InvoiceParser(None, "claude-sonnet-5-5")
    assert not p.available
    with pytest.raises(ParserUnavailable):
        await p.parse("x", CTX)
