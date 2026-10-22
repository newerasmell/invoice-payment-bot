"""What to do with a photo/PDF: issue a similar invoice, save the client, create/update one of
our companies, or turn a supplier's invoice into a payment instruction (Flow D)."""

import asyncio
import logging
import re
import secrets
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.audit import actor_tg, audit
from app.bot import texts
from app.config import Settings
from app.db.models import (
    AllowedUser,
    Company,
    CompanyBankAccount,
    Counterparty,
    InstructionStatus,
    PaymentInstruction,
    UserRole,
)
from app.db.session import Database
from app.iban import iban_is_valid, normalize_iban
from app.invoices import completeness as comp
from app.llm.parser import Attachment, DocumentRead, InvoiceParser, ParserError, PartyDetails
from app.payments import instructions as ins
from app.payments.matching import norm_company_name

log = logging.getLogger(__name__)


class DocFlow(StatesGroup):
    menu = State()  # data: files
    choose_party = State()  # data: doc, purpose
    confirm_company = State()  # data: proposal
    pick_payer = State()  # data: doc, files


class Doc(CallbackData, prefix="dc"):
    action: str  # inv | client | company | pay | party | payer | confirm | cancel
    v: int = 0


class Ins(CallbackData, prefix="in"):
    action: str  # ap | cx | show
    id: int


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=d) for t, d in r] for r in rows
        ]
    )


def _admin(settings: Settings, path: str) -> str | None:
    return (
        f"{settings.public_base_url.rstrip('/')}/admin/{path}" if settings.public_base_url else None
    )


def _files_to_state(files: tuple[Attachment, ...]) -> list[tuple[str, bytes]]:
    return [(f.media_type, f.data) for f in files]


def _files_from_state(raw: list) -> tuple[Attachment, ...]:  # noqa: ANN001
    return tuple(Attachment(m, d) for m, d in raw or [])


def _ours(party: PartyDetails, companies: list[Company]) -> Company | None:
    key = norm_company_name(party.name)
    num = (party.company_number or "").replace(" ", "")
    for c in companies:
        if num and c.eik and c.eik.replace(" ", "") == num:
            return c
        if key and key in {norm_company_name(c.name), norm_company_name(c.legal_name)}:
            return c
    return None


# --- entry points -----------------------------------------------------------------------


async def show_menu(message: Message, state: FSMContext, files: tuple[Attachment, ...]) -> None:
    await state.set_state(DocFlow.menu)
    await state.set_data({"files": _files_to_state(files)})
    await message.answer(
        texts.DOC_MENU,
        reply_markup=_kb(
            [
                [(texts.BTN_DOC_INVOICE, Doc(action="inv").pack())],
                [(texts.BTN_DOC_CLIENT, Doc(action="client").pack())],
                [(texts.BTN_DOC_COMPANY, Doc(action="company").pack())],
                [(texts.BTN_DOC_PAY, Doc(action="pay").pack())],
                [(texts.CANCEL, Doc(action="cancel").pack())],
            ]
        ),
    )


async def run_action(
    message: Message,
    state: FSMContext,
    db: Database,
    parser: InvoiceParser,
    settings: Settings,
    user: AllowedUser,
    intent: str,
    text: str,
    files: tuple[Attachment, ...],
) -> None:
    """client / company / pay on a document (from the menu or recognised from the caption)."""
    if not files:
        await message.answer("Please send the document (photo or PDF) for that.")
        await state.clear()
        return
    status = await message.answer(texts.READING_DOC)
    try:
        doc = await parser.read_document(text, files)
    except ParserError as e:
        await status.edit_text(f"❗ {escape(str(e))}")
        await state.clear()
        return
    await status.delete()
    if intent == "client":
        await _client_from_doc(message, state, db, settings, user, doc)
    elif intent == "company":
        await _company_from_doc(message, state, db, settings, user, doc)
    else:
        await _pay_from_doc(message, state, db, settings, user, doc, files)


# --- 👤 save as client ---------------------------------------------------------------------


async def _client_from_doc(
    message: Message,
    state: FSMContext,
    db: Database,
    settings: Settings,
    user: AllowedUser,
    doc: DocumentRead,
) -> None:
    async with db.session() as s:
        companies = list((await s.scalars(select(Company))).all())
    parties = [p for p in (doc.issuer, doc.recipient) if p.name and not _ours(p, companies)]
    if not parties:
        await state.clear()
        await message.answer(texts.NO_PARTY)
        return
    if len(parties) == 2:
        await state.set_state(DocFlow.choose_party)
        await state.set_data({"doc": doc.model_dump(mode="json"), "purpose": "client"})
        await message.answer(
            texts.PICK_PARTY,
            reply_markup=_kb(
                [
                    [(f"{label}: {p.name[:40]}", Doc(action="party", v=i).pack())]
                    for i, (label, p) in enumerate((("From", doc.issuer), ("To", doc.recipient)))
                ]
                + [[(texts.CANCEL, Doc(action="cancel").pack())]]
            ),
        )
        return
    await _save_client(message, state, db, settings, user, parties[0])


async def _save_client(
    message: Message,
    state: FSMContext,
    db: Database,
    settings: Settings,
    user: AllowedUser,
    party: PartyDetails,
) -> None:
    await state.clear()
    async with db.session() as s:
        clients = (await s.scalars(select(Counterparty))).all()
        num = (party.company_number or "").replace(" ", "")
        key = norm_company_name(party.name)
        client = next(
            (c for c in clients if num and (c.eik or "").replace(" ", "") == num), None
        ) or next((c for c in clients if key and norm_company_name(c.name) == key), None)
        created = client is None
        if created:
            client = Counterparty(name=party.name)
            s.add(client)
        changed = comp.apply_details(client, party)
        await s.flush()
        audit(
            s,
            actor_tg(user.telegram_id),
            "client.created" if created else "client.updated",
            f"client:{client.id}",
            {"fields": changed, "via": "document"},
        )
        await s.commit()
        missing = comp.labels(comp.missing_client(client))
        text = texts.client_saved(
            client, created, changed, missing, _admin(settings, f"clients/{client.id}")
        )
    await message.answer(text)


# --- 🏢 create/update our company ------------------------------------------------------------

_COMPANY_FIELDS = [
    ("name", "legal name"),
    ("company_number", "company number"),
    ("vat_id", "VAT number"),
    ("address", "address"),
    ("city", "city"),
    ("country", "country"),
    ("email", "email"),
    ("contact_name", "represented by"),
]
_TO_COMPANY = {
    "name": "legal_name",
    "company_number": "eik",
    "vat_id": "vat_id",
    "address": "address",
    "city": "city",
    "country": "country",
    "email": "email_from",
    "contact_name": "manager",
}


def suggest_code(name: str, taken: set[str]) -> str:
    words = [
        w
        for w in re.findall(r"[A-Za-z0-9]+", norm_company_name(name) and name or "")
        if w.lower()
        not in {"ltd", "limited", "llc", "inc", "ood", "eood", "ead", "ad", "plc", "gmbh", "sa"}
    ]
    base = ("".join(w[0] for w in words)[:4] or "CO").upper()
    if len(base) < 2 and words:
        base = words[0][:3].upper()
    code, n = base, 2
    while code in taken:
        code, n = f"{base}{n}", n + 1
    return code


async def _company_from_doc(
    message: Message,
    state: FSMContext,
    db: Database,
    settings: Settings,
    user: AllowedUser,
    doc: DocumentRead,
) -> None:
    async with db.session() as s:
        companies = list(
            (await s.scalars(select(Company).options(selectinload(Company.bank_accounts)))).all()
        )
    candidates = [
        (p, _ours(p, companies), i == 0)
        for i, p in enumerate((doc.issuer, doc.recipient))
        if p.name
    ]
    matched = [c for c in candidates if c[1] is not None]
    if matched:
        party, company, is_issuer = matched[0]
    elif candidates:
        party, company, is_issuer = candidates[0]  # the issuer / the company described
    else:
        await state.clear()
        await message.answer(texts.NO_PARTY)
        return
    fields = {}
    for src, label in _COMPANY_FIELDS:
        value = getattr(party, src)
        attr = _TO_COMPANY[src]
        if value and (company is None or not getattr(company, attr)):
            fields[label] = value
    bank = None
    iban = normalize_iban(doc.issuer_iban or "") if is_issuer else ""
    has_iban = company is not None and any(a.iban == iban for a in company.bank_accounts)
    if iban and iban_is_valid(iban) and not has_iban:
        cur = doc.currency or (company.default_currency if company else "EUR")
        bank = f"{iban} ({cur}{', ' + doc.issuer_bank_name if doc.issuer_bank_name else ''})"
    code = company.code if company else suggest_code(party.name or "", {c.code for c in companies})
    await state.set_state(DocFlow.confirm_company)
    await state.set_data(
        {
            "proposal": {
                "company_id": company.id if company else None,
                "code": code,
                "party": party.model_dump(),
                "iban": iban if bank else None,
                "bic": (doc.issuer_bic or "").replace(" ", "").upper() or None,
                "bank_name": doc.issuer_bank_name,
                "currency": doc.currency or "EUR",
            }
        }
    )
    await message.answer(
        texts.company_proposal(code, fields, bank, company is not None),
        reply_markup=_kb(
            [
                [
                    (texts.BTN_CONFIRM, Doc(action="confirm").pack()),
                    (texts.CANCEL, Doc(action="cancel").pack()),
                ]
            ]
        ),
    )


async def _confirm_company(
    cb: CallbackQuery, state: FSMContext, db: Database, settings: Settings, user: AllowedUser
) -> None:
    p = (await state.get_data()).get("proposal")
    await state.clear()
    if not p:
        await cb.message.edit_text(texts.FLOW_EXPIRED)
        return
    party = PartyDetails.model_validate(p["party"])
    async with db.session() as s:
        company = (
            await s.scalar(
                select(Company)
                .options(selectinload(Company.bank_accounts))
                .where(Company.id == p["company_id"])
            )
            if p["company_id"]
            else None
        )
        created = company is None
        if created:
            taken = set((await s.scalars(select(Company.code))).all())
            code = p["code"] if p["code"] not in taken else suggest_code(party.name or "", taken)
            company = Company(
                code=code,
                name=party.name or code,
                invoice_prefix="",
                invoice_next_number=1,
                default_currency=p["currency"],
            )
            company.bank_accounts = []
            s.add(company)
        for src, attr in _TO_COMPANY.items():
            value = getattr(party, src)
            if value and not getattr(company, attr):
                setattr(company, attr, value)
        if p.get("iban") and not any(a.iban == p["iban"] for a in company.bank_accounts):
            company.bank_accounts.append(
                CompanyBankAccount(
                    label=f"{p['currency']}{' - ' + p['bank_name'] if p.get('bank_name') else ''}",
                    currency=p["currency"],
                    iban=p["iban"],
                    bic=p.get("bic"),
                    bank_name=p.get("bank_name"),
                    is_default=not any(a.currency == p["currency"] for a in company.bank_accounts),
                )
            )
        await s.flush()
        audit(
            s,
            actor_tg(user.telegram_id),
            "company.created" if created else "company.updated",
            f"company:{company.id}",
            {"code": company.code, "via": "document"},
        )
        await s.commit()
        code, cid = company.code, company.id
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(
        texts.company_saved(code, created, _admin(settings, f"companies/{cid}"))
    )


# --- 💳 invoice to pay → payment instruction -------------------------------------------------


async def _pay_from_doc(
    message: Message,
    state: FSMContext,
    db: Database,
    settings: Settings,
    user: AllowedUser,
    doc: DocumentRead,
    files: tuple[Attachment, ...],
) -> None:
    async with db.session() as s:
        companies = list((await s.scalars(select(Company).order_by(Company.code))).all())
    if user.companies:
        allowed = {c.upper() for c in user.companies}
        companies = [c for c in companies if c.code in allowed]
    payer = _ours(doc.recipient, companies) if doc.recipient.name else None
    if payer is None and len(companies) == 1:
        payer = companies[0]
    if payer is None:
        await state.set_state(DocFlow.pick_payer)
        await state.set_data({"doc": doc.model_dump(mode="json"), "files": _files_to_state(files)})
        await message.answer(
            texts.PICK_PAYER,
            reply_markup=_kb(
                [
                    [(f"{c.code} · {c.legal_name or c.name}", Doc(action="payer", v=c.id).pack())]
                    for c in companies
                ]
                + [[(texts.CANCEL, Doc(action="cancel").pack())]]
            ),
        )
        return
    await _create_instruction(message, state, db, settings, user, doc, files, payer.id)


async def _store_document(settings: Settings, files: tuple[Attachment, ...]) -> str | None:
    if not files:
        return None
    f = files[0]
    ext = {
        "application/pdf": ".pdf",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(f.media_type, ".jpg")
    folder = settings.uploads_dir / "documents"
    path = folder / f"{secrets.token_hex(8)}{ext}"
    await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_bytes, f.data)
    return str(path)


async def _create_instruction(
    message: Message,
    state: FSMContext,
    db: Database,
    settings: Settings,
    user: AllowedUser,
    doc: DocumentRead,
    files: tuple[Attachment, ...],
    payer_id: int,
) -> None:
    await state.clear()
    async with db.session() as s:
        payer = await s.get(Company, payer_id)
        try:
            instr = await ins.create_from_document(
                s,
                settings,
                payer=payer,
                doc=doc,
                document_path=await _store_document(settings, files),
                created_by=user.telegram_id,
            )
        except ins.InstructionError as e:
            await message.answer(f"❗ {escape(str(e))}")
            return
        await s.commit()
        text = texts.instruction_card(instr, payer)
        kb = card_kb(instr)
    await message.answer(text, reply_markup=kb)


def card_kb(i: PaymentInstruction) -> InlineKeyboardMarkup | None:
    if i.status == InstructionStatus.PROPOSED:
        return _kb(
            [
                [
                    (texts.BTN_APPROVE_PAYMENT, Ins(action="ap", id=i.id).pack()),
                    (texts.BTN_CANCEL_PAYMENT, Ins(action="cx", id=i.id).pack()),
                ]
            ]
        )
    if i.status in (InstructionStatus.APPROVED, InstructionStatus.AWAITING_BANK):
        return _kb([[(texts.BTN_CANCEL_PAYMENT, Ins(action="cx", id=i.id).pack())]])
    return None


# --- callbacks --------------------------------------------------------------------------------


async def on_doc(
    cb: CallbackQuery,
    callback_data: Doc,
    state: FSMContext,
    db: Database,
    parser: InvoiceParser,
    settings: Settings,
    allowed_user: AllowedUser,
) -> None:
    await cb.answer()
    data = await state.get_data()
    act = callback_data.action
    if act == "cancel":
        await state.clear()
        await cb.message.edit_text(texts.CANCELLED)
        return
    current = await state.get_state()
    if act in ("inv", "client", "company", "pay"):
        if current != DocFlow.menu.state:
            await cb.message.edit_text(texts.FLOW_EXPIRED)
            return
        files = _files_from_state(data.get("files"))
        await cb.message.edit_reply_markup(reply_markup=None)
        if act == "inv":
            from app.bot.invoice_flow import start_invoice

            await state.clear()
            await start_invoice(
                cb.message, state, db, parser, settings, allowed_user, "", files, force_invoice=True
            )
            return
        await run_action(cb.message, state, db, parser, settings, allowed_user, act, "", files)
        return
    if act == "party" and current == DocFlow.choose_party.state:
        doc = DocumentRead.model_validate(data["doc"])
        party = (doc.issuer, doc.recipient)[min(callback_data.v, 1)]
        await cb.message.edit_reply_markup(reply_markup=None)
        await _save_client(cb.message, state, db, settings, allowed_user, party)
        return
    if act == "confirm" and current == DocFlow.confirm_company.state:
        await _confirm_company(cb, state, db, settings, allowed_user)
        return
    if act == "payer" and current == DocFlow.pick_payer.state:
        await cb.message.edit_reply_markup(reply_markup=None)
        await _create_instruction(
            cb.message,
            state,
            db,
            settings,
            allowed_user,
            DocumentRead.model_validate(data["doc"]),
            _files_from_state(data.get("files")),
            callback_data.v,
        )
        return
    await cb.message.edit_text(texts.FLOW_EXPIRED)


async def on_instruction(
    cb: CallbackQuery, callback_data: Ins, db: Database, allowed_user: AllowedUser
) -> None:
    async with db.session() as s:
        instr = await s.get(PaymentInstruction, callback_data.id)
        payer = await s.get(Company, instr.from_company_id) if instr else None
        if instr is None or payer is None:
            await cb.answer("Instruction not found", show_alert=True)
            return
        if allowed_user.companies and payer.code not in {c.upper() for c in allowed_user.companies}:
            await cb.answer(texts.NOT_YOUR_COMPANY, show_alert=True)
            return
        if callback_data.action == "show":
            await cb.answer()
            await cb.message.answer(
                texts.instruction_card(instr, payer), reply_markup=card_kb(instr)
            )
            return
        try:
            if callback_data.action == "ap":
                if allowed_user.role != UserRole.APPROVER:
                    await cb.answer(texts.ONLY_APPROVERS_PAYMENT, show_alert=True)
                    return
                await ins.approve(s, instr, allowed_user.telegram_id)
            elif callback_data.action == "cx":
                await ins.cancel(s, instr, actor_tg(allowed_user.telegram_id))
        except ins.InstructionError as e:
            await cb.answer(str(e), show_alert=True)
            return
        await s.commit()
        card, kb = texts.instruction_card(instr, payer), card_kb(instr)
        follow = (
            texts.instruction_approved(instr, payer)
            if callback_data.action == "ap"
            else texts.INSTRUCTION_CANCELLED
        )
    await cb.answer()
    await cb.message.edit_text(card, reply_markup=kb)
    await cb.message.answer(follow)


async def cmd_instructions(message: Message, db: Database) -> None:
    async with db.session() as s:
        rows = (
            await s.execute(
                select(PaymentInstruction, Company.code)
                .join(Company, Company.id == PaymentInstruction.from_company_id)
                .where(PaymentInstruction.status.in_(ins.OPEN))
                .order_by(
                    PaymentInstruction.due_date.is_(None),
                    PaymentInstruction.due_date,
                    PaymentInstruction.id,
                )
            )
        ).all()
    if not rows:
        await message.answer(texts.NO_INSTRUCTIONS)
        return
    await message.answer(
        texts.instructions_list(rows),
        reply_markup=_kb(
            [[(f"Show #{i.id}", Ins(action="show", id=i.id).pack())] for i, _ in rows[:10]]
        ),
    )


async def notify_executed(
    bot: Bot, db: Database, instruction_id: int, recipients: list[int]
) -> None:
    async with db.session() as s:
        instr = await s.get(PaymentInstruction, instruction_id)
        payer = await s.get(Company, instr.from_company_id)
        text = texts.instruction_executed(instr, payer)
    for chat in recipients:
        try:
            await bot.send_message(chat, text)
        except Exception:
            log.warning("could not notify %s", chat, exc_info=True)


def make_router() -> Router:
    r = Router(name="documents")
    r.message.register(cmd_instructions, Command("instructions"))
    r.callback_query.register(on_doc, Doc.filter())
    r.callback_query.register(on_instruction, Ins.filter())
    # A new file while a document menu is open just starts over with that file.
    r.message.register(
        _restart_menu,
        StateFilter(
            DocFlow.menu, DocFlow.choose_party, DocFlow.confirm_company, DocFlow.pick_payer
        ),
        F.photo | F.document,
    )
    return r


async def _restart_menu(message: Message, state: FSMContext) -> None:
    from app.bot.invoice_flow import _attachments

    files = await _attachments(message)
    if isinstance(files, str):
        await message.answer(files)
        return
    await show_menu(message, state, files)
