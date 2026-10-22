"""Telegram invoice flow: free text -> draft PDF -> approve/edit/reject -> send to client."""

import logging
from datetime import date
from html import escape
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.audit import actor_tg, audit
from app.bot import texts
from app.bot.approvals import ask_second_approver, user_label
from app.companies import counterparty_for_company
from app.config import Settings
from app.db.models import (
    AllowedUser,
    Company,
    Counterparty,
    Invoice,
    InvoiceStatus,
    UserRole,
)
from app.db.session import Database
from app.delivery.service import client_channels, deliver
from app.invoices import completeness as comp
from app.invoices import service as svc
from app.invoices.pdf import PdfUnavailable
from app.llm.parser import (
    SUPPORTED_MEDIA,
    Attachment,
    InvoiceDraft,
    InvoiceParser,
    ParseContext,
    ParserError,
)
from app.payments.matching import norm_company_name

log = logging.getLogger(__name__)


class Flow(StatesGroup):
    picking = State()  # waiting for issuer/client buttons
    collecting = State()  # waiting for missing client details
    editing = State()  # waiting for an edit request for data["invoice_id"]


class Pick(CallbackData, prefix="pk"):
    kind: str  # iss | cli | co (one of our companies as client) | new | cancel
    id: int = 0


MAX_FILE_MB = 10


class Inv(CallbackData, prefix="iv"):
    action: str  # ap | ed | rj | sd | ns
    id: int


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=d) for t, d in r] for r in rows
        ]
    )


def draft_kb(inv_id: int) -> InlineKeyboardMarkup:
    return _kb(
        [
            [
                (texts.BTN_APPROVE, Inv(action="ap", id=inv_id).pack()),
                (texts.BTN_EDIT, Inv(action="ed", id=inv_id).pack()),
                (texts.BTN_REJECT, Inv(action="rj", id=inv_id).pack()),
            ]
        ]
    )


def send_kb(inv_id: int) -> InlineKeyboardMarkup:
    return _kb(
        [
            [
                (texts.BTN_SEND, Inv(action="sd", id=inv_id).pack()),
                (texts.BTN_NOT_NOW, Inv(action="ns", id=inv_id).pack()),
            ]
        ]
    )


# --- helpers ------------------------------------------------------------------------------


async def _companies_for(s: AsyncSession, user: AllowedUser) -> list[Company]:
    q = select(Company).options(selectinload(Company.bank_accounts)).order_by(Company.code)
    items = list((await s.scalars(q)).all())
    if user.companies:
        allowed = {c.upper() for c in user.companies}
        items = [c for c in items if c.code in allowed]
    return items


async def _clients(s: AsyncSession) -> list[Counterparty]:
    q = select(Counterparty).where(Counterparty.archived.is_(False)).order_by(Counterparty.name)
    return list((await s.scalars(q)).all())


def _match_client(
    clients: list[Counterparty], companies: list[Company], draft: InvoiceDraft
) -> Counterparty | Company | None:
    """A known client, or one of OUR companies acting as the client (by code or name)."""
    if draft.client_id is not None:
        found = next((c for c in clients if c.id == draft.client_id), None)
        if found:
            return found
    if draft.client_company_code:
        code = draft.client_company_code.strip().upper()
        found_co = next((c for c in companies if c.code == code), None)
        if found_co:
            return found_co
    if draft.client_text:
        text = draft.client_text.strip()
        key = norm_company_name(text)
        for co in companies:
            if text.upper() == co.code or key in {
                norm_company_name(co.name),
                norm_company_name(co.legal_name),
            }:
                return co
        exact = [c for c in clients if norm_company_name(c.name) == key and key]
        if len(exact) == 1:
            return exact[0]
    return None


def _match_issuer(companies: list[Company], draft: InvoiceDraft) -> Company | None:
    if draft.issuer_code:
        found = next((c for c in companies if c.code == draft.issuer_code.upper()), None)
        if found:
            return found
    return companies[0] if len(companies) == 1 else None


def _line_inputs(draft: InvoiceDraft) -> list[svc.LineInput]:
    return [
        svc.LineInput(li.description, li.quantity, li.unit_price, li.unit) for li in draft.lines
    ]


def _warnings(b: svc.InvoiceBundle, unclear: str | None) -> list[str]:
    w = []
    if b.invoice.bank_account_id is None:
        w.append(f"{b.issuer.code} has no bank account in {b.invoice.currency}")
    if not client_channels(b):
        w.append(f"{b.client.name} has no email/Telegram for delivery yet")
    if unclear:
        w.append(unclear)
    return w


async def send_draft_preview(
    bot: Bot,
    chat_id: int,
    db: Database,
    inv_id: int,
    unclear: str | None = None,
    header: str | None = None,
) -> None:
    """Draft PDF with Approve / Edit / Reject buttons (text only if the PDF engine is down)."""
    async with db.session() as s:
        b = await svc.load_bundle(s, inv_id)
        caption = texts.draft_caption(b.invoice, b.issuer, b.client, _warnings(b, unclear))
        if header:
            caption = f"{header}\n{caption}"[:1000]
        try:
            pdf = await svc.render_bundle_pdf(s, b)
        except PdfUnavailable as e:
            log.error("draft preview without PDF: %s", e)
            await bot.send_message(
                chat_id,
                caption + "\n" + texts.PDF_UNAVAILABLE.format(error=escape(str(e))),
                reply_markup=draft_kb(inv_id),
            )
            return
    await bot.send_document(
        chat_id,
        BufferedInputFile(pdf, filename=svc.pdf_filename(b.invoice)),
        caption=caption,
        reply_markup=draft_kb(inv_id),
    )


async def _send_preview(
    message: Message, db: Database, inv_id: int, unclear: str | None = None
) -> None:
    await send_draft_preview(message.bot, message.chat.id, db, inv_id, unclear)


async def _attachments(message: Message) -> tuple[Attachment, ...] | str:
    """Files on the message as Claude inputs, or a user-facing error string."""
    if message.photo:
        file_id, mime, size = message.photo[-1].file_id, "image/jpeg", message.photo[-1].file_size
    elif message.document:
        doc = message.document
        file_id, mime, size = doc.file_id, (doc.mime_type or "").lower(), doc.file_size
        if mime == "image/jpg":
            mime = "image/jpeg"
        if mime not in SUPPORTED_MEDIA:
            return texts.UNSUPPORTED_FILE
    else:
        return ()
    if size and size > MAX_FILE_MB * 1024 * 1024:
        return texts.FILE_TOO_BIG.format(mb=MAX_FILE_MB)
    buf = await message.bot.download(file_id)
    assert buf is not None
    return (Attachment(mime, buf.read()),)


def _message_text(message: Message) -> str:
    return message.text or message.caption or ""


async def _parse(
    text: str,
    user: AllowedUser,
    db: Database,
    parser: InvoiceParser,
    current: dict[str, Any] | None = None,
    attachments: tuple[Attachment, ...] = (),
) -> tuple[InvoiceDraft, list, list, list]:
    async with db.session() as s:
        companies = await _companies_for(s, user)
        all_companies = list((await s.scalars(select(Company).order_by(Company.code))).all())
        clients = await _clients(s)
    ctx = ParseContext(
        companies=[(c.code, c.legal_name or c.name) for c in all_companies],
        clients=[(c.id, c.name) for c in clients],
        today=date.today(),
        current_draft=current,
    )
    draft = await parser.parse(text, ctx, attachments)
    return draft, companies, clients, all_companies


# --- new request ----------------------------------------------------------------------------


async def on_text(
    message: Message,
    state: FSMContext,
    db: Database,
    parser: InvoiceParser,
    settings: Settings,
    allowed_user: AllowedUser,
) -> None:
    if not parser.available:
        await message.answer(texts.PARSER_DISABLED)
        return
    files = await _attachments(message)
    if isinstance(files, str):
        await message.answer(files)
        return
    from app.bot import documents_flow  # avoid an import cycle

    text = _message_text(message)
    if files and not text.strip():
        await documents_flow.show_menu(message, state, files)
        return
    await start_invoice(message, state, db, parser, settings, allowed_user, text, files)


async def start_invoice(
    message: Message,
    state: FSMContext,
    db: Database,
    parser: InvoiceParser,
    settings: Settings,
    allowed_user: AllowedUser,
    text: str,
    files: tuple[Attachment, ...],
    force_invoice: bool = False,
) -> None:
    from app.bot import documents_flow  # avoid an import cycle

    status = await message.answer(texts.READING)
    try:
        draft, companies, clients, all_companies = await _parse(
            text, allowed_user, db, parser, attachments=files
        )
    except ParserError as e:
        await status.edit_text(f"❗ {escape(str(e))}")
        return
    if force_invoice and draft.intent != "invoice":
        draft.intent = "invoice"
    if draft.intent in ("pay", "client", "company"):
        await status.delete()
        await documents_flow.run_action(
            message, state, db, parser, settings, allowed_user, draft.intent, text, files
        )
        return
    if draft.intent != "invoice":
        await status.edit_text(texts.NOT_AN_INVOICE)
        return
    if not draft.lines:
        await status.edit_text(
            texts.NEED_AMOUNT + (f"\n{escape(draft.unclear)}" if draft.unclear else "")
        )
        return
    if not companies:
        await status.edit_text(texts.NO_COMPANIES_FOR_USER)
        return
    issuer = _match_issuer(companies, draft)
    client = _match_client(clients, all_companies, draft)
    client_id = client.id if isinstance(client, Counterparty) else None
    if isinstance(client, Company):
        async with db.session() as s:
            client_id = (await counterparty_for_company(s, client)).id
            await s.commit()
    await state.set_state(Flow.picking)
    await state.set_data(
        {
            "draft": draft.model_dump(mode="json"),
            "issuer_id": issuer.id if issuer else None,
            "client_id": client_id,
            "confirm_name": False,
        }
    )
    await status.delete()
    await _advance(message, state, db, settings, allowed_user)


async def _advance(
    message: Message, state: FSMContext, db: Database, settings: Settings, user: AllowedUser
) -> None:
    data = await state.get_data()
    draft = InvoiceDraft.model_validate(data["draft"])
    async with db.session() as s:
        companies = await _companies_for(s, user)
        if data.get("issuer_id") is None:
            await message.answer(
                texts.PICK_ISSUER,
                reply_markup=_kb(
                    [
                        [(f"{c.code} · {c.name}", Pick(kind="iss", id=c.id).pack())]
                        for c in companies
                    ]
                    + [[(texts.CANCEL, Pick(kind="cancel").pack())]]
                ),
            )
            return
        if data.get("client_id") is None:
            clients = [c for c in await _clients(s) if c.company_id is None]
            key = (draft.client_text or "").casefold()
            ranked = sorted(clients, key=lambda c: (key not in c.name.casefold(), c.name))[:6]
            rows = [[(c.name, Pick(kind="cli", id=c.id).pack())] for c in ranked]
            ours = await s.scalars(
                select(Company).where(Company.id != data["issuer_id"]).order_by(Company.code)
            )
            rows += [
                [(f"🏢 {c.code} · {c.legal_name or c.name}", Pick(kind="co", id=c.id).pack())]
                for c in ours
            ]
            if draft.client_text:
                rows.append(
                    [
                        (
                            texts.CREATE_CLIENT.format(name=draft.client_text[:40]),
                            Pick(kind="new").pack(),
                        )
                    ]
                )
            rows.append([(texts.CANCEL, Pick(kind="cancel").pack())])
            hint = f" (you wrote “{escape(draft.client_text)}”)" if draft.client_text else ""
            await message.answer(texts.PICK_CLIENT.format(hint=hint), reply_markup=_kb(rows))
            return
        issuer = next((c for c in companies if c.id == data["issuer_id"]), None)
        client = await s.get(Counterparty, data["client_id"])
        if issuer is None or client is None:
            await state.clear()
            await message.answer(texts.FLOW_EXPIRED)
            return
        # Use whatever the request/document said about the client to fill empty fields.
        changed = comp.apply_details(client, draft.client_details)
        if changed:
            audit(
                s,
                actor_tg(user.telegram_id),
                "client.updated",
                f"client:{client.id}",
                {"fields": changed, "via": "telegram"},
            )
            await s.commit()
        issuer_gaps = comp.missing_issuer(issuer, draft.currency or issuer.default_currency)
        if issuer_gaps:
            await state.clear()
            link = (
                f"{settings.public_base_url.rstrip('/')}/admin/companies/{issuer.id}"
                if settings.public_base_url
                else None
            )
            await message.answer(texts.issuer_incomplete(issuer.code, issuer_gaps, link))
            return
        client_gaps = comp.missing_client(client, bool(data.get("confirm_name")))
        if client_gaps:
            await state.set_state(Flow.collecting)
            await state.update_data(asking=client_gaps)
            await message.answer(
                texts.ask_client_details(client.name, comp.labels(client_gaps), _known(client))
            )
            return
        try:
            inv = await svc.create_draft(
                s,
                settings,
                issuer=issuer,
                client=client,
                lines=_line_inputs(draft),
                currency=draft.currency,
                vat_rate=draft.vat_rate,
                include_vat=draft.amounts_include_vat,
                due_in_days=draft.due_in_days,
                notes=draft.notes,
                created_by=user.telegram_id,
            )
        except svc.InvoiceError as e:
            await state.clear()
            await message.answer(f"❗ {escape(str(e))}")
            return
        await s.commit()
        inv_id = inv.id
    await state.clear()
    await _send_preview(message, db, inv_id, draft.unclear)


async def on_pick(
    cb: CallbackQuery,
    callback_data: Pick,
    state: FSMContext,
    db: Database,
    settings: Settings,
    allowed_user: AllowedUser,
) -> None:
    await cb.answer()
    if await state.get_state() != Flow.picking.state:
        await cb.message.edit_text(texts.FLOW_EXPIRED)
        return
    data = await state.get_data()
    if callback_data.kind == "cancel":
        await state.clear()
        await cb.message.edit_text(texts.CANCELLED)
        return
    if callback_data.kind == "iss":
        data["issuer_id"] = callback_data.id
    elif callback_data.kind == "cli":
        data["client_id"] = callback_data.id
    elif callback_data.kind == "co":
        async with db.session() as s:
            company = await s.get(Company, callback_data.id)
            if company is None:
                await cb.message.edit_text(texts.FLOW_EXPIRED)
                return
            data["client_id"] = (await counterparty_for_company(s, company)).id
            await s.commit()
    elif callback_data.kind == "new":
        draft = InvoiceDraft.model_validate(data["draft"])
        details = draft.client_details
        name = ((details.name if details else None) or draft.client_text or "").strip()
        # A name typed in the chat may be a nickname/abbreviation: confirm the legal name.
        data["confirm_name"] = not (details and details.name)
        async with db.session() as s:
            client = Counterparty(name=name)
            comp.apply_details(client, details)
            s.add(client)
            await s.flush()
            audit(
                s,
                actor_tg(allowed_user.telegram_id),
                "client.created",
                f"client:{client.id}",
                {"name": name, "via": "telegram"},
            )
            await s.commit()
            data["client_id"] = client.id
    await state.set_data(data)
    await cb.message.delete()
    await _advance(cb.message, state, db, settings, allowed_user)


def _known(c: Counterparty) -> dict[str, str]:
    fields = [
        ("name", "name"),
        ("eik", "company no"),
        ("vat_id", "VAT"),
        ("address", "address"),
        ("city", "city"),
        ("country", "country"),
    ]
    return {label: getattr(c, attr) for attr, label in fields if getattr(c, attr)}


async def on_collect(
    message: Message,
    state: FSMContext,
    db: Database,
    parser: InvoiceParser,
    settings: Settings,
    allowed_user: AllowedUser,
) -> None:
    """The operator answers with missing client details (text, photo or PDF)."""
    data = await state.get_data()
    asking: list[str] = data.get("asking") or []
    files = await _attachments(message)
    if isinstance(files, str):
        await message.answer(files)
        return
    text = _message_text(message).strip()
    async with db.session() as s:
        client = await s.get(Counterparty, data.get("client_id"))
        if client is None:
            await state.clear()
            await message.answer(texts.FLOW_EXPIRED)
            return
        if parser.available:
            try:
                details = await parser.extract_party(
                    text, _known(client), [comp.labels([a]) for a in asking], files
                )
            except ParserError as e:
                await message.answer(f"❗ {escape(str(e))}")
                return
            changed = comp.apply_details(client, details, overwrite=set(asking))
        elif len(asking) == 1 and text:
            setattr(client, asking[0], text)  # no Claude: take the reply as the one asked value
            changed = asking[:1]
        else:
            changed = []
        if changed:
            audit(
                s,
                actor_tg(allowed_user.telegram_id),
                "client.updated",
                f"client:{client.id}",
                {"fields": changed, "via": "telegram"},
            )
            await s.commit()
        confirm_name = bool(data.get("confirm_name")) and "name" not in changed
        remaining = comp.missing_client(client, confirm_name)
        name, known = client.name, _known(client)
    await state.update_data(confirm_name=confirm_name, asking=remaining)
    if changed:
        await message.answer(texts.details_saved(name, comp.labels(changed)))
    if remaining:
        question = texts.ask_client_details(name, comp.labels(remaining), known)
        await message.answer(
            question if changed else texts.COLLECT_NOTHING_FOUND.format(question=question)
        )
        return
    await state.set_state(Flow.picking)
    await _advance(message, state, db, settings, allowed_user)


# --- draft actions --------------------------------------------------------------------------


async def _guard_company(user: AllowedUser, issuer: Company, cb: CallbackQuery) -> bool:
    if user.companies and issuer.code not in {c.upper() for c in user.companies}:
        await cb.answer(texts.NOT_YOUR_COMPANY, show_alert=True)
        return False
    return True


async def on_invoice_action(
    cb: CallbackQuery,
    callback_data: Inv,
    state: FSMContext,
    db: Database,
    settings: Settings,
    allowed_user: AllowedUser,
    bot: Bot,
) -> None:
    action, inv_id = callback_data.action, callback_data.id
    actor = actor_tg(allowed_user.telegram_id)
    if action in ("ap", "rj") and (await state.get_data()).get("invoice_id") == inv_id:
        await state.clear()  # approving/rejecting ends a pending edit of the same draft
    async with db.session() as s:
        try:
            b = await svc.load_bundle(s, inv_id)
        except svc.InvoiceError as e:
            await cb.answer(str(e), show_alert=True)
            return
        if not await _guard_company(allowed_user, b.issuer, cb):
            return

        if action == "ap":
            if allowed_user.role != UserRole.APPROVER:
                await cb.answer(texts.ONLY_APPROVERS, show_alert=True)
                return
            try:
                pdf = await svc.approve(s, settings, b, allowed_user.telegram_id)
            except svc.InvoiceError as e:
                await cb.answer(str(e), show_alert=True)
                return
            except PdfUnavailable as e:
                await cb.answer(f"Cannot approve: {e}", show_alert=True)
                return
            if pdf is None:
                await cb.answer("First approval recorded")
                await cb.message.reply(
                    texts.first_approval_done(
                        "this invoice",
                        b.invoice.amount_gross,
                        b.invoice.currency,
                        settings.max_invoice_amount,
                    )
                )
                text = (
                    texts.second_approval_request(
                        f"invoice draft #{inv_id}", user_label(allowed_user)
                    )
                    + "\n\n"
                    + texts.draft_caption(b.invoice, b.issuer, b.client, [])
                )
                if not await ask_second_approver(
                    bot, db, b.issuer, allowed_user.telegram_id, text, draft_kb(inv_id)
                ):
                    await cb.message.answer(texts.NO_SECOND_APPROVER)
                return
            await cb.answer("Approved")
            await cb.message.edit_reply_markup(reply_markup=None)
            link = svc.public_url(settings, b.invoice)
            await cb.message.answer_document(
                BufferedInputFile(pdf, filename=svc.pdf_filename(b.invoice)),
                caption=texts.approved_caption(b.invoice, b.issuer, b.client, link),
                reply_markup=send_kb(inv_id),
            )
            return

        if action == "rj":
            try:
                await svc.reject(s, b, actor)
                await s.commit()
            except svc.InvoiceError as e:
                await cb.answer(str(e), show_alert=True)
                return
            await cb.answer("Rejected")
            await cb.message.edit_reply_markup(reply_markup=None)
            await cb.message.reply("❌ Draft rejected.")
            return

        if action == "ed":
            if b.invoice.status != InvoiceStatus.DRAFT:
                await cb.answer("Only drafts can be edited", show_alert=True)
                return
            if b.invoice.kind == svc.CREDIT_NOTE:
                await cb.answer(
                    "A credit note mirrors its invoice. Reject it and create it again instead.",
                    show_alert=True,
                )
                return
            await cb.answer()
            await state.set_state(Flow.editing)
            await state.set_data({"invoice_id": inv_id})
            await cb.message.reply(texts.EDIT_PROMPT)
            return

        if action == "ns":
            await cb.answer()
            await cb.message.edit_reply_markup(reply_markup=None)
            await cb.message.reply(texts.NOT_NOW)
            return

        if action == "sd":
            try:
                results = await deliver(
                    s,
                    settings,
                    bot,
                    b,
                    actor_tg=allowed_user.telegram_id,
                    operator_chat_id=cb.message.chat.id,
                )
                await s.commit()
            except svc.InvoiceError as e:
                await cb.answer(str(e), show_alert=True)
                return
            await cb.answer("Done")
            await cb.message.edit_reply_markup(reply_markup=None)
            await cb.message.reply(
                texts.delivery_report(b.invoice.number or "", results, settings.dry_run)
            )
            return
    await cb.answer()


async def on_edit_text(
    message: Message,
    state: FSMContext,
    db: Database,
    parser: InvoiceParser,
    settings: Settings,
    allowed_user: AllowedUser,
) -> None:
    inv_id = (await state.get_data()).get("invoice_id")
    if not parser.available:
        await state.clear()
        await message.answer(texts.PARSER_DISABLED)
        return
    async with db.session() as s:
        try:
            b = await svc.load_bundle(s, inv_id)
        except svc.InvoiceError:
            b = None
        if b is None or b.invoice.status != InvoiceStatus.DRAFT:
            # The draft was approved/rejected meanwhile: leave edit mode and treat the message
            # as a new request instead of failing on every message.
            await state.clear()
            await message.answer(texts.EDIT_STOPPED)
            await on_text(message, state, db, parser, settings, allowed_user)
            return
        current = svc.draft_as_dict(b)
    files = await _attachments(message)
    if isinstance(files, str):
        await message.answer(files)
        return
    status = await message.answer(texts.READING)
    try:
        draft, companies, clients, all_companies = await _parse(
            _message_text(message), allowed_user, db, parser, current, files
        )
    except ParserError as e:
        await status.edit_text(f"❗ {escape(str(e))}")
        return
    async with db.session() as s:
        b = await svc.load_bundle(s, inv_id)
        issuer = _match_issuer(companies, draft) or b.issuer
        match = _match_client(clients, all_companies, draft)
        client = await counterparty_for_company(s, match) if isinstance(match, Company) else match
        note = None
        if client is None:
            client = b.client
            if draft.client_text and draft.client_text.casefold() != b.client.name.casefold():
                note = texts.EDIT_FAILED_CLIENT.format(name=b.client.name)
        issuer = await s.scalar(
            select(Company)
            .options(selectinload(Company.bank_accounts))
            .where(Company.id == issuer.id)
        )
        client = await s.get(Counterparty, client.id)
        try:
            await svc.update_draft(
                s,
                settings,
                b,
                issuer=issuer,
                client=client,
                lines=_line_inputs(draft),
                currency=draft.currency,
                vat_rate=draft.vat_rate,
                include_vat=draft.amounts_include_vat,
                due_in_days=draft.due_in_days,
                notes=draft.notes,
                actor=actor_tg(allowed_user.telegram_id),
            )
            await s.commit()
        except svc.InvoiceError as e:
            await status.edit_text(f"❗ {escape(str(e))}")
            return
    await state.clear()
    await status.delete()
    await _send_preview(message, db, inv_id, note or draft.unclear)


async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(texts.CANCELLED)


async def cmd_credit(
    message: Message, command: CommandObject, bot: Bot, db: Database, allowed_user: AllowedUser
) -> None:
    number, _, reason = (command.args or "").strip().partition(" ")
    if not number:
        await message.answer(texts.CREDIT_USAGE)
        return
    async with db.session() as s:
        inv_id = await s.scalar(
            select(Invoice.id).where(Invoice.number == number, Invoice.kind == "invoice")
        )
        if inv_id is None:
            await message.answer(texts.CREDIT_NOT_FOUND.format(number=escape(number)))
            return
        b = await svc.load_bundle(s, inv_id)
        if allowed_user.companies and b.issuer.code not in {
            c.upper() for c in allowed_user.companies
        }:
            await message.answer(texts.NOT_YOUR_COMPANY)
            return
        try:
            note = await svc.create_credit_note(
                s, b, reason=reason, created_by=allowed_user.telegram_id
            )
        except svc.InvoiceError as e:
            await message.answer(f"❗ {escape(str(e))}")
            return
        await s.commit()
        note_id = note.id
        header = texts.CREDIT_HEADER.format(
            number=escape(number),
            date=b.invoice.issue_date.strftime("%d.%m.%Y") if b.invoice.issue_date else "",
        )
    await send_draft_preview(bot, message.chat.id, db, note_id, header=header)


async def cmd_invoices(message: Message, db: Database) -> None:
    async with db.session() as s:
        rows = (
            await s.execute(
                select(Invoice, Company.code, Counterparty.name)
                .join(Company, Company.id == Invoice.issuer_id)
                .join(Counterparty, Counterparty.id == Invoice.counterparty_id)
                .where(Invoice.status != InvoiceStatus.CANCELLED)
                .order_by(Invoice.id.desc())
                .limit(10)
            )
        ).all()
    if not rows:
        await message.answer(texts.NO_INVOICES)
        return
    buttons = [
        [(f"📤 Send {inv.number}", Inv(action="sd", id=inv.id).pack())]
        for inv, _, _ in rows
        if inv.status == InvoiceStatus.APPROVED
    ]
    await message.answer(texts.invoice_list(rows), reply_markup=_kb(buttons) if buttons else None)


def make_router() -> Router:
    r = Router(name="invoices")
    r.message.register(cmd_cancel, Command("cancel", "stop"))
    r.message.register(cmd_invoices, Command("invoices"))
    r.message.register(cmd_credit, Command("credit"))
    content = (F.text & ~F.text.startswith("/")) | F.photo | F.document
    r.message.register(on_edit_text, StateFilter(Flow.editing), content)
    r.message.register(on_collect, StateFilter(Flow.collecting), content)
    r.message.register(on_text, content)
    r.callback_query.register(on_pick, Pick.filter())
    r.callback_query.register(on_invoice_action, Inv.filter())
    return r
