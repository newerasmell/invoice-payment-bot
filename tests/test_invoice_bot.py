from decimal import Decimal

from aiogram.methods import AnswerCallbackQuery, SendDocument, SendMessage
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.bot.invoice_flow import Inv, Pick
from app.db.models import AllowedUser, Counterparty, Invoice, InvoiceStatus, UserRole
from app.llm.parser import InvoiceDraft, ParserError
from tests.conftest import APPROVER_ID, BotHarness

OPERATOR_ID = 2002


def draft(**kw) -> InvoiceDraft:  # noqa: ANN003
    base = {
        "intent": "invoice",
        "issuer_code": "Z",
        "client_text": "Client Ltd",
        "lines": [{"description": "Consulting", "quantity": "1", "unit_price": "1200"}],
    }
    return InvoiceDraft.model_validate({**base, **kw})


def docs(calls: list) -> list[SendDocument]:
    return [c for c in calls if isinstance(c, SendDocument)]


def buttons(call) -> list[str]:  # noqa: ANN001
    kb = call.reply_markup.inline_keyboard if call.reply_markup else []
    return [b.callback_data for row in kb for b in row]


async def _client(db, **kw) -> int:  # noqa: ANN001, ANN003
    async with db.session() as s:
        c = Counterparty(
            name="Client Ltd", email="ap@client.example", delivery_channels=["email"], **kw
        )
        s.add(c)
        await s.commit()
        return c.id


async def test_full_flow_text_to_sent(harness: BotHarness, db) -> None:
    cid = await _client(db)
    harness.parser.queue.append(draft(client_id=cid))
    calls = await harness.send_raw("Invoice Client Ltd 1200 EUR from Z for consulting")

    # The parser saw our companies and clients as context.
    _, ctx = harness.parser.calls[0]
    assert ("Z", "Company Z Ltd") in ctx.companies and (cid, "Client Ltd") in ctx.clients

    [preview] = docs(calls)
    assert preview.document.filename.startswith("draft-")
    assert "Draft invoice" in preview.caption and "1,440.00 EUR" in preview.caption
    inv_id = int(buttons(preview)[0].rsplit(":", 1)[1])
    assert buttons(preview) == [Inv(action=a, id=inv_id).pack() for a in ("ap", "ed", "rj")]

    calls = await harness.click(Inv(action="ap", id=inv_id).pack())
    [final] = docs(calls)
    assert final.document.filename == "invoice-0000000001.pdf"
    assert "Invoice 0000000001" in final.caption and "https://bot.example.com/i/" in final.caption
    assert buttons(final) == [Inv(action=a, id=inv_id).pack() for a in ("sd", "ns")]

    # Double click on Approve is harmless.
    calls = await harness.click(Inv(action="ap", id=inv_id).pack())
    assert any(
        isinstance(c, AnswerCallbackQuery) and "already approved" in (c.text or "") for c in calls
    )

    calls = await harness.click(Inv(action="sd", id=inv_id).pack())
    report = [c.text for c in calls if isinstance(c, SendMessage)][0]
    assert "DRY RUN" in report and "email → ap@client.example: dry_run" in report
    async with db.session() as s:
        inv = await s.get(Invoice, inv_id)
        assert inv.status == InvoiceStatus.SENT and inv.created_by == APPROVER_ID


async def test_pick_issuer_and_create_client(harness: BotHarness, db) -> None:
    harness.parser.queue.append(draft(issuer_code=None, client_text="Brand New Ltd"))
    calls = await harness.send_raw("invoice brand new 1200")
    [ask] = [c for c in calls if isinstance(c, SendMessage) and "Which of our" in c.text]
    assert len(buttons(ask)) == 4  # X, Z, G + cancel
    async with db.session() as s:
        from app.db.models import Company

        z = await s.scalar(select(Company.id).where(Company.code == "Z"))
    calls = await harness.click(Pick(kind="iss", id=z).pack())
    [ask] = [c for c in calls if isinstance(c, SendMessage) and "Who is the client" in c.text]
    assert Pick(kind="new").pack() in buttons(ask)
    calls = await harness.click(Pick(kind="new").pack())
    [preview] = docs(calls)
    assert "Z → Brand New Ltd" in preview.caption
    assert "has no email/Telegram" in preview.caption
    async with db.session() as s:
        assert await s.scalar(select(Counterparty).where(Counterparty.name == "Brand New Ltd"))


async def test_cancel_pick(harness: BotHarness, db) -> None:
    harness.parser.queue.append(draft(issuer_code=None))
    await harness.send_raw("invoice")
    calls = await harness.click(Pick(kind="cancel").pack())
    assert any(getattr(c, "text", None) == "Cancelled." for c in calls)
    calls = await harness.click(Pick(kind="iss", id=1).pack())
    assert any("expired" in (getattr(c, "text", "") or "") for c in calls)


async def test_edit_and_reject(harness: BotHarness, db) -> None:
    cid = await _client(db)
    harness.parser.queue.append(draft(client_id=cid))
    calls = await harness.send_raw("invoice")
    inv_id = int(buttons(docs(calls)[0])[0].rsplit(":", 1)[1])

    await harness.click(Inv(action="ed", id=inv_id).pack())
    harness.parser.queue.append(
        draft(
            client_id=cid,
            vat_rate="0",
            lines=[{"description": "Consulting", "quantity": "2", "unit_price": "750"}],
        )
    )
    calls = await harness.send_raw("make it 2 x 750 with no VAT")
    _, ctx = harness.parser.calls[-1]
    assert ctx.current_draft["lines"][0]["unit_price"] == "1200.00"
    [preview] = docs(calls)
    assert "1,500.00 EUR" in preview.caption
    async with db.session() as s:
        assert (await s.get(Invoice, inv_id)).amount_gross == Decimal("1500.00")

    await harness.click(Inv(action="rj", id=inv_id).pack())
    async with db.session() as s:
        assert (await s.get(Invoice, inv_id)).status == InvoiceStatus.CANCELLED


async def test_operator_cannot_approve(harness: BotHarness, db) -> None:
    async with db.session() as s:
        s.add(AllowedUser(telegram_id=OPERATOR_ID, role=UserRole.OPERATOR))
        await s.commit()
    cid = await _client(db)
    harness.parser.queue.append(draft(client_id=cid))
    calls = await harness.send_raw("invoice", user_id=OPERATOR_ID)
    inv_id = int(buttons(docs(calls)[0])[0].rsplit(":", 1)[1])
    calls = await harness.click(Inv(action="ap", id=inv_id).pack(), user_id=OPERATOR_ID)
    assert any(
        isinstance(c, AnswerCallbackQuery) and "Only approvers" in (c.text or "") for c in calls
    )
    # Strangers can't press buttons either.
    assert await harness.click(Inv(action="ap", id=inv_id).pack(), user_id=6666) == []


async def test_parser_errors_and_non_invoice(harness: BotHarness) -> None:
    harness.parser.queue += [
        ParserError("Claude API error 500"),
        InvoiceDraft(intent="other"),
        draft(lines=[]),
    ]
    assert "Claude API error 500" in (await harness.send("hello"))[-1]
    assert "I can prepare invoices" in (await harness.send("what's up"))[-1]
    assert "at least one item" in (await harness.send("invoice Client"))[-1]


async def test_invoices_command(harness: BotHarness, db) -> None:
    assert (await harness.send("/invoices"))[0] == "No invoices yet."
    cid = await _client(db)
    harness.parser.queue.append(draft(client_id=cid))
    calls = await harness.send_raw("invoice")
    inv_id = int(buttons(docs(calls)[0])[0].rsplit(":", 1)[1])
    await harness.click(Inv(action="ap", id=inv_id).pack())
    calls = await harness.send_raw("/invoices")
    [msg] = [c for c in calls if isinstance(c, SendMessage)]
    assert "0000000001 · Z → Client Ltd" in msg.text and "APPROVED" in msg.text
    assert buttons(msg) == [Inv(action="sd", id=inv_id).pack()]


async def test_public_link(harness: BotHarness, db, settings) -> None:
    from app.main import create_app

    cid = await _client(db)
    harness.parser.queue.append(draft(client_id=cid))
    calls = await harness.send_raw("invoice")
    inv_id = int(buttons(docs(calls)[0])[0].rsplit(":", 1)[1])
    async with db.session() as s:
        assert (await s.get(Invoice, inv_id)).public_token is None  # drafts have no link
    await harness.click(Inv(action="ap", id=inv_id).pack())
    async with db.session() as s:
        token = (await s.get(Invoice, inv_id)).public_token

    app_settings = settings.model_copy(update={"telegram_bot_token": None})
    with TestClient(create_app(app_settings)) as c:
        r = c.get(f"/i/{token}")
        assert r.status_code == 200 and r.content.startswith(b"%PDF")
        assert r.headers["content-type"] == "application/pdf"
        assert "noindex" in r.headers["x-robots-tag"]
        assert c.get("/i/" + "x" * 32).status_code == 404
        assert c.get("/i/short").status_code == 404
        health = c.get("/health").json()
        assert health["pdf"].startswith("weasyprint") and health["llm"] is False
