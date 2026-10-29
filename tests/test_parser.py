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

    async def fake_call(system: str, prompt: str, schema: dict, attachments=()) -> str:  # noqa: ANN001
        return queue.pop(0)

    p._call = fake_call  # type: ignore[method-assign]
    return p


async def test_valid_output() -> None:
    d = await parser_returning(json.dumps(GOOD)).parse("Invoice Client Ltd 1200 EUR from Z", CTX)
    assert d.issuer_code == "Z" and d.client_id == 7 and d.currency == "EUR"
    assert d.lines[0].unit_price == Decimal("1200.00") and d.vat_rate == Decimal("20")


async def test_retries_once_with_feedback_then_fails() -> None:
    prompts: list[str] = []
    p = InvoiceParser("test-key", "claude-sonnet-5-5")
    queue = ["not json", json.dumps(GOOD)]

    async def fake_call(system: str, prompt: str, schema: dict, attachments=()) -> str:  # noqa: ANN001
        prompts.append(prompt)
        return queue.pop(0)

    p._call = fake_call  # type: ignore[method-assign]
    d = await p.parse("x", CTX)
    assert d.lines[0].unit_price == Decimal("1200.00")
    assert "previous answer was rejected" in prompts[1]
    with pytest.raises(ParserError, match="rephrase"):
        await parser_returning("not json", "{}").parse("x", CTX)


@pytest.mark.parametrize(
    ("patch", "field", "expected"),
    [
        ({"currency": "EURO"}, "currency", "EUR"),
        ({"currency": "dollars"}, "currency", "USD"),
        ({"currency": "Monopoly money"}, "currency", None),
        ({"vat_rate": "20%"}, "vat_rate", Decimal("20")),
        ({"vat_rate": "150"}, "vat_rate", None),
        ({"due_in_days": "30 days"}, "due_in_days", 30),
        ({"due_in_days": 9999}, "due_in_days", None),
        ({"client_id": "NSOL"}, "client_id", None),
        ({"client_id": "7"}, "client_id", 7),
    ],
)
def test_odd_values_degrade_instead_of_failing(patch: dict, field: str, expected) -> None:  # noqa: ANN001
    assert getattr(InvoiceDraft.model_validate({**GOOD, **patch}), field) == expected


def test_lines_are_lenient_but_never_invent() -> None:
    def lines(*items: dict) -> list:
        return InvoiceDraft.model_validate({**GOOD, "lines": list(items)}).lines

    # "invoice for 36k from TPL to NSOL": no description, quantity left empty, shorthand amount.
    [li] = lines({"description": "", "quantity": "", "unit_price": "36k", "unit": ""})
    assert (li.description, li.quantity, li.unit_price) == ("", Decimal("1"), Decimal("36000.00"))
    assert lines({"description": "x", "quantity": "2 pcs", "unit_price": "1.5m", "unit": ""})[
        0
    ].unit_price == Decimal("1500000.00")
    # Lines with no usable price or a negative quantity are dropped, never guessed.
    bad = [
        {"description": "x", "quantity": "1", "unit_price": "", "unit": ""},
        {"description": "x", "quantity": "1", "unit_price": "abc", "unit": ""},
        {"description": "x", "quantity": "1", "unit_price": "0", "unit": ""},
        {"description": "x", "quantity": "-1", "unit_price": "5", "unit": ""},
    ]
    assert lines(*bad) == []


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


def _unions(x: object) -> int:
    if isinstance(x, dict):
        own = 1 if "anyOf" in x or isinstance(x.get("type"), list) else 0
        return own + sum(_unions(v) for v in x.values())
    if isinstance(x, list):
        return sum(_unions(v) for v in x)
    return 0


def test_schema_has_no_union_types() -> None:
    """The API rejects schemas with many nullable/anyOf fields (it hit 20 in production);
    optional values are plain strings where "" means not provided."""
    from app.llm.parser import PARTY_SCHEMA

    assert _unions(DRAFT_SCHEMA) == 0 and _unions(PARTY_SCHEMA) == 0


def test_blank_strings_mean_none() -> None:
    blank = {
        k: ""
        for k in (
            "name",
            "company_number",
            "vat_id",
            "address",
            "city",
            "country",
            "email",
            "contact_name",
        )
    }
    raw = {
        **GOOD,
        "issuer_code": "",
        "client_id": "",
        "client_company_code": "NSOL",
        "currency": "",
        "vat_rate": "",
        "due_in_days": "",
        "client_details": blank,
        "lines": [{"description": "Marketing", "quantity": "1", "unit_price": "98000", "unit": ""}],
    }
    d = InvoiceDraft.model_validate(raw)
    assert (d.issuer_code, d.client_id, d.currency, d.vat_rate, d.due_in_days) == (
        None,
        None,
        None,
        None,
        None,
    )
    assert d.client_company_code == "NSOL" and d.client_details.filled() == {}
    assert d.lines[0].unit is None
    d2 = InvoiceDraft.model_validate({**GOOD, "client_id": "7", "due_in_days": "30"})
    assert (d2.client_id, d2.due_in_days) == (7, 30)
