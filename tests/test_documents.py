from decimal import Decimal

from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.bot.documents_flow import Doc, Ins, suggest_code
from app.db.models import (
    Company,
    Counterparty,
    InstructionStatus,
    Invoice,
    PaymentInstruction,
)
from app.llm.parser import DocumentRead, InvoiceDraft, PartyDetails
from tests.conftest import APPROVER_ID, BotHarness

PDF = ("invoice.pdf", "application/pdf", b"%PDF-1.4 test")
SUPPLIER = PartyDetails(
    name="Trade Peak Limited",
    company_number="15596647",
    address="262 Uxbridge Road",
    city="London",
    country="United Kingdom",
)
LOOPLYX = PartyDetails(
    name="Looplyx Ltd",
    company_number="16591090",
    address="262 Uxbridge Rd",
    city="Hatch End",
    country="United Kingdom",
    email="ap@looplyx.example",
)
OUR_X = PartyDetails(name="Company X Ltd", company_number="200000001")


def doc(**kw) -> DocumentRead:  # noqa: ANN003
    base = dict(
        doc_type="invoice",
        issuer=SUPPLIER,
        recipient=OUR_X,
        issuer_iban="BG80BNBG96611020345678",
        issuer_bic="BNBGBGSD",
        issuer_bank_name="Paynetics",
        invoice_number="INV-2026-005",
        issue_date="2026-10-12",
        due_date="2026-10-22",
        currency="EUR",
        total="1200.00",
        payment_reference="",
    )
    return DocumentRead.model_validate({**base, **kw})


def msgs(calls: list) -> list[str]:
    return [c.text for c in calls if isinstance(c, SendMessage | EditMessageText)]


def kb(call) -> list[str]:  # noqa: ANN001
    m = call.reply_markup
    return [b.callback_data for row in m.inline_keyboard for b in row] if m else []


async def _menu(harness: BotHarness) -> None:
    calls = await harness.send_raw("", document=PDF)
    [menu] = [c for c in calls if isinstance(c, SendMessage)]
    assert menu.text == "What should I do with this document?"
    assert kb(menu) == [Doc(action=a).pack() for a in ("inv", "client", "company", "pay", "cancel")]


async def test_save_client_from_document(harness: BotHarness, db) -> None:
    await _menu(harness)
    harness.parser.doc_queue.append(doc(issuer=LOOPLYX, recipient=OUR_X))
    out = msgs(await harness.click(Doc(action="client").pack()))
    assert "New client saved: <b>Looplyx Ltd</b>" in out[-1] and "16591090" in out[-1]
    assert "/admin/clients/" in out[-1] and "Still missing" not in out[-1]
    # Again with more details: updates blanks only, no duplicate.
    await _menu(harness)
    harness.parser.doc_queue.append(
        doc(
            issuer=LOOPLYX.model_copy(update={"vat_id": "GB123", "name": "LOOPLYX LTD"}),
            recipient=OUR_X,
        )
    )
    out = msgs(await harness.click(Doc(action="client").pack()))
    assert "Client updated" in out[-1] and "Filled: vat_id" in out[-1]
    async with db.session() as s:
        clients = (await s.scalars(select(Counterparty))).all()
        assert [(c.name, c.vat_id) for c in clients] == [("Looplyx Ltd", "GB123")]


async def test_client_asks_which_party_when_both_unknown(harness: BotHarness, db) -> None:
    await _menu(harness)
    harness.parser.doc_queue.append(doc(issuer=SUPPLIER, recipient=LOOPLYX))
    calls = await harness.click(Doc(action="client").pack())
    [ask] = [c for c in calls if isinstance(c, SendMessage) and "Which company" in c.text]
    assert Doc(action="party", v=1).pack() in kb(ask)
    out = msgs(await harness.click(Doc(action="party", v=1).pack()))
    assert "Looplyx Ltd" in out[-1]


async def test_create_our_company_from_document(harness: BotHarness, db) -> None:
    await _menu(harness)
    harness.parser.doc_queue.append(doc(issuer=SUPPLIER, recipient=LOOPLYX))
    calls = await harness.click(Doc(action="company").pack())
    [prop] = [c for c in calls if isinstance(c, SendMessage) and "Create our company" in c.text]
    assert "<b>TP</b>" in prop.text and "BG80BNBG96611020345678" in prop.text
    async with db.session() as s:
        assert await s.scalar(select(Company).where(Company.code == "TP")) is None  # not yet
    out = msgs(await harness.click(Doc(action="confirm").pack()))
    assert "Company <b>TP</b> created" in out[-1]
    async with db.session() as s:
        tp = await s.scalar(
            select(Company).options(selectinload(Company.bank_accounts)).where(Company.code == "TP")
        )
        assert (tp.legal_name, tp.eik, tp.city) == ("Trade Peak Limited", "15596647", "London")
        [acc] = tp.bank_accounts
        assert (acc.iban, acc.currency, acc.is_default, acc.bic) == (
            "BG80BNBG96611020345678",
            "EUR",
            True,
            "BNBGBGSD",
        )


async def test_update_existing_company_fills_blanks_only(harness: BotHarness, db) -> None:
    async with db.session() as s:
        x = await s.scalar(select(Company).where(Company.code == "X"))
        x.vat_id = None
        await s.commit()
    await _menu(harness)
    harness.parser.doc_queue.append(
        doc(
            doc_type="company_details",
            recipient=PartyDetails(),
            issuer=OUR_X.model_copy(update={"vat_id": "BG999", "city": "Varna"}),
            issuer_iban="",
        )
    )
    calls = await harness.click(Doc(action="company").pack())
    [prop] = [c for c in calls if isinstance(c, SendMessage) and "Update our company" in c.text]
    assert "VAT number: BG999" in prop.text and "Varna" not in prop.text  # city already set
    await harness.click(Doc(action="confirm").pack())
    async with db.session() as s:
        x = await s.scalar(select(Company).where(Company.code == "X"))
        assert (x.vat_id, x.city) == ("BG999", "Sofia")


async def test_invoice_to_pay_full_cycle(harness: BotHarness, db, settings) -> None:
    await _menu(harness)
    harness.parser.doc_queue.append(doc(total="1200.00"))
    calls = await harness.click(Doc(action="pay").pack())
    [card] = [c for c in calls if isinstance(c, SendMessage) and "Payment instruction" in c.text]
    assert "From: <b>X</b>" in card.text and "To: <b>Trade Peak Limited</b>" in card.text
    assert (
        "<code>BG80BNBG96611020345678</code>" in card.text and "<code>1200.00</code>" in card.text
    )
    assert "due <b>22.10.2026</b>" in card.text
    async with db.session() as s:
        instr = await s.scalar(select(PaymentInstruction))
        assert instr.status == InstructionStatus.PROPOSED and instr.document_path
        assert instr.reference == "INV-2026-005"
    assert kb(card) == [Ins(action="ap", id=instr.id).pack(), Ins(action="cx", id=instr.id).pack()]

    calls = await harness.click(Ins(action="ap", id=instr.id).pack())
    assert any("Paynetics panel of X" in t for t in msgs(calls))
    async with db.session() as s:
        assert (await s.get(PaymentInstruction, instr.id)).status == InstructionStatus.AWAITING_BANK

    # The operator pays in the bank; polling sees the outgoing transfer.
    calls = await harness.send_raw(
        '/dev_simulate_outgoing X 1200 "Trade Peak Limited" BG80BNBG96611020345678 INV-2026-005'
    )
    assert any("Payment executed" in t and "Trade Peak Limited" in t for t in msgs(calls))
    async with db.session() as s:
        got = await s.get(PaymentInstruction, instr.id)
        assert got.status == InstructionStatus.EXECUTED and got.matched_transfer_id
    assert msgs(await harness.send_raw("/instructions"))[-1] == "No open payment instructions."


async def test_pay_validation_and_duplicates(harness: BotHarness, db) -> None:
    for bad, err in [
        (doc(issuer_iban="BG80BNBG96611020345679"), "checksum"),
        (doc(total=""), "amount due"),
    ]:
        await _menu(harness)
        harness.parser.doc_queue.append(bad)
        assert err in msgs(await harness.click(Doc(action="pay").pack()))[-1]
    await _menu(harness)
    harness.parser.doc_queue.append(doc())
    await harness.click(Doc(action="pay").pack())
    await _menu(harness)
    harness.parser.doc_queue.append(doc())
    assert (
        "already has payment instruction" in msgs(await harness.click(Doc(action="pay").pack()))[-1]
    )


async def test_large_payment_needs_second_approver(harness: BotHarness, db) -> None:
    from app.db.models import AllowedUser, UserRole

    async with db.session() as s:
        s.add(AllowedUser(telegram_id=2003, role=UserRole.APPROVER, display_name="Maria"))
        s.add(AllowedUser(telegram_id=2004, role=UserRole.APPROVER, companies=["Z"]))
        await s.commit()
    await _menu(harness)
    harness.parser.doc_queue.append(doc(total="90000"))
    await harness.click(Doc(action="pay").pack())
    async with db.session() as s:
        iid = await s.scalar(select(PaymentInstruction.id))
    calls = await harness.click(Ins(action="ap", id=iid).pack())
    assert any("second approver" in t for t in msgs(calls))
    asks = [c for c in calls if isinstance(c, SendMessage) and "Second approval needed" in c.text]
    assert [c.chat_id for c in asks] == [2003]  # not the first approver, not a Z-only user
    async with db.session() as s:
        got = await s.get(PaymentInstruction, iid)
        assert got.status == InstructionStatus.PROPOSED and got.first_approved_by == APPROVER_ID
    calls = await harness.click(Ins(action="ap", id=iid).pack())
    assert any(
        isinstance(c, AnswerCallbackQuery) and "different approver" in (c.text or "") for c in calls
    )
    async with db.session() as s:
        assert (await s.get(PaymentInstruction, iid)).status == InstructionStatus.PROPOSED
    calls = await harness.click(Ins(action="ap", id=iid).pack(), user_id=2003)
    assert any("Paynetics panel of X" in t for t in msgs(calls))
    async with db.session() as s:
        got = await s.get(PaymentInstruction, iid)
        assert got.status == InstructionStatus.AWAITING_BANK and got.approved_by == 2003


async def test_payer_picker_and_operator_cannot_approve(harness: BotHarness, db) -> None:
    from app.db.models import AllowedUser, UserRole

    await _menu(harness)
    harness.parser.doc_queue.append(doc(recipient=PartyDetails(name="Someone Else Ltd")))
    calls = await harness.click(Doc(action="pay").pack())
    [ask] = [c for c in calls if isinstance(c, SendMessage) and "Which of our" in c.text]
    async with db.session() as s:
        z = await s.scalar(select(Company.id).where(Company.code == "Z"))
        s.add(AllowedUser(telegram_id=2002, role=UserRole.OPERATOR))
        await s.commit()
    assert Doc(action="payer", v=z).pack() in kb(ask)
    calls = await harness.click(Doc(action="payer", v=z).pack())
    assert any("From: <b>Z</b>" in t for t in msgs(calls))
    async with db.session() as s:
        iid = await s.scalar(select(PaymentInstruction.id))
    calls = await harness.click(Ins(action="ap", id=iid).pack(), user_id=2002)
    assert any(
        isinstance(c, AnswerCallbackQuery) and "Only approvers" in (c.text or "") for c in calls
    )
    await harness.click(Ins(action="cx", id=iid).pack(), user_id=2002)
    async with db.session() as s:
        assert (await s.get(PaymentInstruction, iid)).status == InstructionStatus.CANCELLED


async def test_supplier_invoice_from_our_company_is_cross_checked(
    harness: BotHarness, db, settings
) -> None:
    from app.invoices import service as svc
    from tests.conftest import CLIENT_DETAILS

    async with db.session() as s:
        z = await s.scalar(
            select(Company).options(selectinload(Company.bank_accounts)).where(Company.code == "Z")
        )
        c = Counterparty(name="Company X Ltd", **CLIENT_DETAILS)
        s.add(c)
        await s.flush()
        inv = await svc.create_draft(
            s,
            settings,
            issuer=z,
            client=c,
            vat_rate=Decimal(0),
            lines=[svc.LineInput("Fee", Decimal(1), Decimal("985"))],
        )
        await s.commit()
        await svc.approve(s, settings, await svc.load_bundle(s, inv.id), 1001)
        number = (await s.get(Invoice, inv.id)).number
    await _menu(harness)
    harness.parser.doc_queue.append(
        doc(issuer=PartyDetails(name="Company Z Ltd"), invoice_number=number, total="990.00")
    )
    out = msgs(await harness.click(Doc(action="pay").pack()))
    assert f"Our records show invoice {number} for 985.00 EUR" in out[-1]


async def test_caption_routes_to_payment(harness: BotHarness, db) -> None:
    harness.parser.queue.append(InvoiceDraft(intent="pay"))
    harness.parser.doc_queue.append(doc())
    calls = await harness.send_raw("please pay this", photo=b"\xff\xd8jpeg")
    assert any("Payment instruction #" in t for t in msgs(calls))
    assert harness.parser.calls[-1][0] == "doc" and harness.parser.calls[-1][1] == "please pay this"


async def test_menu_issue_similar_invoice(harness: BotHarness) -> None:
    await _menu(harness)
    harness.parser.queue.append(InvoiceDraft(intent="other"))
    out = msgs(await harness.click(Doc(action="inv").pack()))
    # Even if the model hesitates, the menu choice means "invoice": it asks for amounts.
    assert any("at least one item" in t for t in out)
    _, _, files = harness.parser.calls[-1]
    assert files[0].media_type == "application/pdf"


def test_suggest_code() -> None:
    assert suggest_code("Trade Peak Limited", set()) == "TP"
    assert suggest_code("Trade Peak Limited", {"TP"}) == "TP2"
    assert suggest_code("NOVE SOLIDA LTD", set()) == "NS"
