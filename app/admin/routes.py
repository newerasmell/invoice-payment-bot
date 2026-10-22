import asyncio
import logging
import secrets
from collections.abc import AsyncIterator
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.datastructures import FormData, UploadFile

from app.admin import forms as f
from app.admin.auth import check_csrf, csrf_token, password_matches, require_admin
from app.audit import audit
from app.bank.base import BankError
from app.bank.paynetics import PayneticsClient
from app.companies import counterparty_for_company, default_bank_account
from app.config import Settings
from app.db.models import (
    AllowedUser,
    AuditLog,
    BankTransfer,
    Company,
    CompanyBankAccount,
    Contract,
    Counterparty,
    FeeRuleType,
    Invoice,
    InvoiceTemplate,
    NotePreset,
    Rule,
    RuleRun,
    RuleType,
    UserRole,
)
from app.invoices import service as inv_svc
from app.invoices.options import FONTS, LAYOUTS, TemplateOptions
from app.invoices.render import render_invoice_html
from app.invoices.view import sample_invoice
from app.redact import redact_iban
from app.rules.engine import PAYOUT_MODES

log = logging.getLogger(__name__)
ACTOR = "admin:web"
LOGO_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/svg+xml": ".svg"}
MAX_LOGO_BYTES = 1_000_000

STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["redact_iban"] = redact_iban
templates.env.globals["LAYOUTS"] = LAYOUTS
templates.env.globals["FONTS"] = FONTS
templates.env.globals["PAYOUT_MODES"] = PAYOUT_MODES
templates.env.filters["env_code"] = lambda code: (code or "").upper().replace("-", "_")

public = APIRouter(prefix="/admin")
router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.db.session() as s:
        yield s


DB = Annotated[AsyncSession, Depends(get_session)]
Cfg = Annotated[Settings, Depends(get_settings)]


def render(request: Request, name: str, status_code: int = 200, **ctx: Any) -> HTMLResponse:
    ctx.setdefault("flash", request.session.pop("flash", None))
    ctx["csrf"] = csrf_token(request)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def redirect(url: str, flash: str | None = None, request: Request | None = None) -> Response:
    if flash and request is not None:
        request.session["flash"] = flash
    return RedirectResponse(url, status_code=303)


async def posted(request: Request) -> FormData:
    form = await request.form()
    check_csrf(request, form)
    return form


async def load_company(session: AsyncSession, company_id: int) -> Company:
    company = await session.scalar(
        select(Company)
        .options(selectinload(Company.bank_accounts), selectinload(Company.templates))
        .where(Company.id == company_id)
    )
    if company is None:
        raise HTTPException(404, "Company not found")
    return company


# --- auth -----------------------------------------------------------------------


@public.get("/login")
async def login_form(request: Request, settings: Cfg) -> Response:
    return render(request, "login.html", enabled=settings.admin_password is not None)


@public.post("/login")
async def login(request: Request, settings: Cfg) -> Response:
    form = await posted(request)
    given = f.text(form, "password") or ""
    if settings.admin_password and password_matches(
        given, settings.admin_password.get_secret_value()
    ):
        request.session.clear()
        request.session["admin"] = True
        return redirect("/admin")
    await asyncio.sleep(0.5)  # slow down guessing
    log.warning("failed admin login from %s", request.client.host if request.client else "?")
    return render(
        request,
        "login.html",
        401,
        enabled=settings.admin_password is not None,
        error="Wrong password",
    )


@public.post("/logout")
async def logout(request: Request) -> Response:
    await posted(request)
    request.session.clear()
    return redirect("/admin/login")


# --- companies ------------------------------------------------------------------


@router.get("")
async def companies_index(request: Request, session: DB) -> Response:
    items = (
        await session.scalars(
            select(Company)
            .options(selectinload(Company.bank_accounts), selectinload(Company.templates))
            .order_by(Company.code)
        )
    ).all()
    return render(request, "companies.html", companies=items)


_PROFILE_TEXT = (
    "name",
    "legal_name",
    "eik",
    "vat_id",
    "address",
    "city",
    "country",
    "manager",
    "phone",
    "website",
    "email_from",
    "no_vat_note",
)


def _parse_company(form: FormData, errors: f.FormErrors) -> dict[str, Any]:
    data: dict[str, Any] = {k: f.text(form, k) for k in _PROFILE_TEXT}
    if not data["name"]:
        errors["name"] = "Required"
    data["code"] = f.company_code(f.text(form, "code"), errors)
    data["invoice_prefix"] = f.text(form, "invoice_prefix") or ""
    data["invoice_next_number"] = f.integer(form, "invoice_next_number", errors, minimum=1)
    data["default_currency"] = f.currency(
        f.text(form, "default_currency"), errors, "default_currency"
    )
    data["default_vat_rate"] = f.percent(form, "default_vat_rate", errors)
    data["default_payment_terms_days"] = f.integer(form, "default_payment_terms_days", errors)
    return data


@router.get("/companies/new")
async def company_new(request: Request) -> Response:
    blank = Company(
        invoice_next_number=1,
        default_currency="EUR",
        default_vat_rate=20,
        default_payment_terms_days=14,
        invoice_prefix="",
    )
    return render(request, "company_form.html", company=blank, values={}, errors={})


@router.post("/companies/new")
async def company_create(request: Request, session: DB) -> Response:
    form = await posted(request)
    errors = f.FormErrors()
    data = _parse_company(form, errors)
    if not errors and await session.scalar(select(Company.id).where(Company.code == data["code"])):
        errors["code"] = "Another company already uses this code"
    if errors:
        return render(
            request, "company_form.html", 422, company=Company(), values=dict(form), errors=errors
        )
    company = Company(**data)
    session.add(company)
    await session.flush()
    audit(session, ACTOR, "company.created", f"company:{company.id}", {"code": company.code})
    await session.commit()
    return redirect(f"/admin/companies/{company.id}", "Company created", request)


@router.get("/companies/{company_id}")
async def company_edit(request: Request, company_id: int, session: DB) -> Response:
    company = await load_company(session, company_id)
    return render(request, "company_form.html", company=company, values={}, errors={})


@router.post("/companies/{company_id}")
async def company_update(request: Request, company_id: int, session: DB) -> Response:
    company = await load_company(session, company_id)
    form = await posted(request)
    errors = f.FormErrors()
    data = _parse_company(form, errors)
    if (
        not errors
        and data["code"] != company.code
        and await session.scalar(select(Company.id).where(Company.code == data["code"]))
    ):
        errors["code"] = "Another company already uses this code"
    if errors:
        return render(
            request, "company_form.html", 422, company=company, values=dict(form), errors=errors
        )
    changed = {k: str(v) for k, v in data.items() if getattr(company, k) != v}
    for k, v in data.items():
        setattr(company, k, v)
    audit(session, ACTOR, "company.updated", f"company:{company.id}", changed)
    await session.commit()
    return redirect(f"/admin/companies/{company.id}", "Profile saved", request)


@router.post("/companies/{company_id}/logo")
async def company_logo_upload(
    request: Request, company_id: int, session: DB, settings: Cfg
) -> Response:
    company = await load_company(session, company_id)
    form = await posted(request)
    upload = form.get("logo")
    url = f"/admin/companies/{company.id}"
    if not isinstance(upload, UploadFile) or not upload.filename:
        return redirect(url, "Choose an image file first", request)
    ext = LOGO_TYPES.get(upload.content_type or "")
    if ext is None:
        return redirect(url, "Logo must be PNG, JPG or SVG", request)
    content = await upload.read(MAX_LOGO_BYTES + 1)
    if len(content) > MAX_LOGO_BYTES:
        return redirect(url, "Logo must be under 1 MB", request)
    folder = settings.uploads_dir / "logos"
    await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
    path = folder / f"company-{company.id}-{secrets.token_hex(4)}{ext}"
    await asyncio.to_thread(path.write_bytes, content)
    old = company.logo_path
    company.logo_path = str(path)
    audit(session, ACTOR, "company.logo_uploaded", f"company:{company.id}", {"path": str(path)})
    await session.commit()
    if old:
        await asyncio.to_thread(Path(old).unlink, missing_ok=True)
    return redirect(url, "Logo updated", request)


@router.post("/companies/{company_id}/logo/delete")
async def company_logo_delete(request: Request, company_id: int, session: DB) -> Response:
    company = await load_company(session, company_id)
    await posted(request)
    old, company.logo_path = company.logo_path, None
    audit(session, ACTOR, "company.logo_removed", f"company:{company.id}")
    await session.commit()
    if old:
        await asyncio.to_thread(Path(old).unlink, missing_ok=True)
    return redirect(f"/admin/companies/{company.id}", "Logo removed", request)


@router.get("/companies/{company_id}/logo")
async def company_logo(company_id: int, session: DB) -> Response:
    company = await load_company(session, company_id)
    if not company.logo_path or not await asyncio.to_thread(Path(company.logo_path).is_file):
        raise HTTPException(404)
    return FileResponse(company.logo_path)


# --- bank accounts ----------------------------------------------------------------


def _parse_account(form: FormData, errors: f.FormErrors) -> dict[str, Any]:
    label = f.text(form, "label")
    if not label:
        errors["label"] = "Required"
    return {
        "label": label,
        "currency": f.currency(f.text(form, "currency"), errors),
        "iban": f.iban(f.text(form, "iban"), errors),
        "bic": f.bic(f.text(form, "bic"), errors),
        "bank_name": f.text(form, "bank_name"),
        "bank_account_ref": f.text(form, "bank_account_ref"),
        "is_default": f.checkbox(form, "is_default"),
    }


def _apply_default(company: Company, account: CompanyBankAccount) -> None:
    """Only one default account per currency."""
    if account.is_default:
        for other in company.bank_accounts:
            if other is not account and other.currency == account.currency:
                other.is_default = False


@router.get("/companies/{company_id}/accounts/new")
async def account_new(request: Request, company_id: int, session: DB) -> Response:
    company = await load_company(session, company_id)
    account = CompanyBankAccount(
        currency=company.default_currency, is_default=not company.bank_accounts
    )
    return render(
        request, "account_form.html", company=company, account=account, values={}, errors={}
    )


@router.post("/companies/{company_id}/accounts/new")
async def account_create(request: Request, company_id: int, session: DB) -> Response:
    company = await load_company(session, company_id)
    form = await posted(request)
    errors = f.FormErrors()
    data = _parse_account(form, errors)
    if errors:
        return render(
            request,
            "account_form.html",
            422,
            company=company,
            account=CompanyBankAccount(),
            values=dict(form),
            errors=errors,
        )
    account = CompanyBankAccount(**data)
    company.bank_accounts.append(account)
    _apply_default(company, account)
    await session.flush()
    audit(
        session,
        ACTOR,
        "bank_account.created",
        f"bank_account:{account.id}",
        {"company": company.code, "iban": redact_iban(account.iban)},
    )
    await session.commit()
    return redirect(f"/admin/companies/{company.id}#accounts", "Bank account added", request)


def _find_account(company: Company, account_id: int) -> CompanyBankAccount:
    account = next((a for a in company.bank_accounts if a.id == account_id), None)
    if account is None:
        raise HTTPException(404, "Bank account not found")
    return account


@router.get("/companies/{company_id}/accounts/{account_id}")
async def account_edit(request: Request, company_id: int, account_id: int, session: DB) -> Response:
    company = await load_company(session, company_id)
    account = _find_account(company, account_id)
    return render(
        request, "account_form.html", company=company, account=account, values={}, errors={}
    )


@router.post("/companies/{company_id}/accounts/{account_id}")
async def account_update(
    request: Request, company_id: int, account_id: int, session: DB
) -> Response:
    company = await load_company(session, company_id)
    account = _find_account(company, account_id)
    form = await posted(request)
    errors = f.FormErrors()
    data = _parse_account(form, errors)
    if errors:
        return render(
            request,
            "account_form.html",
            422,
            company=company,
            account=account,
            values=dict(form),
            errors=errors,
        )
    for k, v in data.items():
        setattr(account, k, v)
    _apply_default(company, account)
    audit(
        session,
        ACTOR,
        "bank_account.updated",
        f"bank_account:{account.id}",
        {"company": company.code, "iban": redact_iban(account.iban)},
    )
    await session.commit()
    return redirect(f"/admin/companies/{company.id}#accounts", "Bank account saved", request)


@router.post("/companies/{company_id}/accounts/{account_id}/delete")
async def account_delete(
    request: Request, company_id: int, account_id: int, session: DB
) -> Response:
    company = await load_company(session, company_id)
    account = _find_account(company, account_id)
    await posted(request)
    await session.delete(account)
    audit(
        session,
        ACTOR,
        "bank_account.deleted",
        f"bank_account:{account_id}",
        {"company": company.code, "iban": redact_iban(account.iban)},
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return redirect(
            f"/admin/companies/{company_id}#accounts",
            "This account is used by existing invoices and cannot be deleted",
            request,
        )
    return redirect(f"/admin/companies/{company_id}#accounts", "Bank account deleted", request)


# --- invoice templates -------------------------------------------------------------


def _options_from_form(form: FormData) -> tuple[str, TemplateOptions | None, dict[str, str]]:
    layout = f.text(form, "layout") or "classic"
    raw = {
        "accent_color": f.text(form, "accent_color") or "",
        "font": f.text(form, "font") or "",
        "title": f.text(form, "title") or "INVOICE",
        "logo_position": f.text(form, "logo_position") or "left",
        "header_note": f.text(form, "header_note") or "",
        "footer_note": f.text(form, "footer_note") or "",
        "subtitle": f.text(form, "subtitle") or "",
        **{
            k: f.checkbox(form, k)
            for k in (
                "show_bank_details",
                "show_vat_breakdown",
                "show_signature",
                "show_due_date",
                "show_line_numbers",
                "show_amount_in_words",
                "show_company_footer",
            )
        },
    }
    errors: dict[str, str] = {}
    if layout not in LAYOUTS:
        errors["layout"] = "Unknown layout"
    try:
        options = TemplateOptions.model_validate(raw)
    except ValidationError as e:
        for err in e.errors():
            errors[str(err["loc"][0])] = err["msg"].removeprefix("Value error, ")
        options = None
    return layout, options, errors


async def _load_template(session: AsyncSession, template_id: int) -> InvoiceTemplate:
    tpl = await session.get(InvoiceTemplate, template_id)
    if tpl is None:
        raise HTTPException(404, "Template not found")
    return tpl


@router.post("/companies/{company_id}/templates/new")
async def template_create(request: Request, company_id: int, session: DB) -> Response:
    company = await load_company(session, company_id)
    form = await posted(request)
    base_name = f.text(form, "name") or "New template"
    names = {t.name for t in company.templates}
    name, n = base_name, 2
    while name in names:
        name, n = f"{base_name} ({n})", n + 1
    tpl = InvoiceTemplate(name=name, layout="classic", options=TemplateOptions().model_dump())
    company.templates.append(tpl)
    await session.flush()
    if company.default_template_id is None:
        company.default_template_id = tpl.id
    audit(session, ACTOR, "template.created", f"template:{tpl.id}", {"company": company.code})
    await session.commit()
    return redirect(f"/admin/templates/{tpl.id}")


@router.get("/templates/{template_id}")
async def template_edit(request: Request, template_id: int, session: DB) -> Response:
    tpl = await _load_template(session, template_id)
    company = await load_company(session, tpl.company_id)
    options = TemplateOptions.parse_lenient(tpl.options)
    return render(request, "template_editor.html", company=company, tpl=tpl, o=options, errors={})


@router.post("/templates/{template_id}")
async def template_update(request: Request, template_id: int, session: DB) -> Response:
    tpl = await _load_template(session, template_id)
    company = await load_company(session, tpl.company_id)
    form = await posted(request)
    name = f.text(form, "name")
    layout, options, errors = _options_from_form(form)
    if not name:
        errors["name"] = "Required"
    elif any(t.name == name and t.id != tpl.id for t in company.templates):
        errors["name"] = "This company already has a template with this name"
    if errors or options is None:
        shown = options or TemplateOptions.parse_lenient(dict(form))
        return render(
            request, "template_editor.html", 422, company=company, tpl=tpl, o=shown, errors=errors
        )
    tpl.name, tpl.layout, tpl.options = name, layout, options.model_dump()
    if f.checkbox(form, "make_default"):
        company.default_template_id = tpl.id
    audit(
        session,
        ACTOR,
        "template.updated",
        f"template:{tpl.id}",
        {"layout": layout, "options": tpl.options},
    )
    await session.commit()
    return redirect(f"/admin/templates/{tpl.id}", "Template saved", request)


@router.post("/templates/{template_id}/duplicate")
async def template_duplicate(request: Request, template_id: int, session: DB) -> Response:
    tpl = await _load_template(session, template_id)
    company = await load_company(session, tpl.company_id)
    await posted(request)
    names = {t.name for t in company.templates}
    name, n = f"{tpl.name} (copy)", 2
    while name in names:
        name, n = f"{tpl.name} (copy {n})", n + 1
    copy = InvoiceTemplate(name=name, layout=tpl.layout, options=dict(tpl.options))
    company.templates.append(copy)
    await session.flush()
    audit(session, ACTOR, "template.duplicated", f"template:{copy.id}", {"from": tpl.id})
    await session.commit()
    return redirect(f"/admin/templates/{copy.id}", f"Copied as “{name}”", request)


@router.post("/templates/{template_id}/archive")
async def template_archive(request: Request, template_id: int, session: DB) -> Response:
    tpl = await _load_template(session, template_id)
    company = await load_company(session, tpl.company_id)
    await posted(request)
    tpl.archived = not tpl.archived
    if tpl.archived and company.default_template_id == tpl.id:
        company.default_template_id = next(
            (t.id for t in company.templates if not t.archived and t.id != tpl.id), None
        )
    audit(
        session,
        ACTOR,
        "template.archived" if tpl.archived else "template.restored",
        f"template:{tpl.id}",
    )
    await session.commit()
    return redirect(
        f"/admin/companies/{company.id}#templates",
        "Template archived" if tpl.archived else "Template restored",
        request,
    )


@router.post("/templates/{template_id}/default")
async def template_make_default(request: Request, template_id: int, session: DB) -> Response:
    tpl = await _load_template(session, template_id)
    company = await load_company(session, tpl.company_id)
    await posted(request)
    company.default_template_id = tpl.id
    audit(session, ACTOR, "template.made_default", f"template:{tpl.id}")
    await session.commit()
    return redirect(f"/admin/companies/{company.id}#templates", "Default template set", request)


@router.get("/templates/{template_id}/preview")
async def template_preview(request: Request, template_id: int, session: DB) -> Response:
    """Renders a sample invoice. Query params override the saved options (live preview)."""
    tpl = await _load_template(session, template_id)
    company = await load_company(session, tpl.company_id)
    layout, options = tpl.layout, TemplateOptions.parse_lenient(tpl.options)
    if request.query_params.get("live"):
        layout, live, _ = _options_from_form(FormData(request.query_params.multi_items()))
        options = live or TemplateOptions.parse_lenient(dict(request.query_params))
    currency = request.query_params.get("currency")
    bank = default_bank_account(company, currency)
    logo = f"/admin/companies/{company.id}/logo" if company.logo_path else None
    inv = sample_invoice(company, bank, logo)
    return HTMLResponse(render_invoice_html(inv, layout, options))


# --- note presets -----------------------------------------------------------------


@router.get("/notes")
async def notes_index(request: Request, session: DB) -> Response:
    notes = (await session.scalars(select(NotePreset).order_by(NotePreset.title))).all()
    companies = (await session.scalars(select(Company).order_by(Company.code))).all()
    return render(
        request,
        "notes.html",
        notes=notes,
        companies=companies,
        by_id={c.id: c for c in companies},
        edit=None,
        values={},
        errors={},
    )


def _parse_note(form: FormData, errors: f.FormErrors) -> dict[str, Any]:
    title, body = f.text(form, "title"), f.text(form, "text")
    if not title:
        errors["title"] = "Required"
    if not body:
        errors["text"] = "Required"
    company_raw = f.text(form, "company_id")
    return {"title": title, "text": body, "company_id": int(company_raw) if company_raw else None}


@router.post("/notes")
async def note_save(request: Request, session: DB) -> Response:
    form = await posted(request)
    errors = f.FormErrors()
    data = _parse_note(form, errors)
    note_id = f.text(form, "id")
    if errors:
        notes = (await session.scalars(select(NotePreset).order_by(NotePreset.title))).all()
        companies = (await session.scalars(select(Company).order_by(Company.code))).all()
        return render(
            request,
            "notes.html",
            422,
            notes=notes,
            companies=companies,
            by_id={c.id: c for c in companies},
            edit=note_id,
            values=dict(form),
            errors=errors,
        )
    if note_id:
        note = await session.get(NotePreset, int(note_id))
        if note is None:
            raise HTTPException(404)
        for k, v in data.items():
            setattr(note, k, v)
        action = "note.updated"
    else:
        note = NotePreset(**data)
        session.add(note)
        action = "note.created"
    await session.flush()
    audit(session, ACTOR, action, f"note:{note.id}", {"title": note.title})
    await session.commit()
    return redirect("/admin/notes", "Note saved", request)


@router.post("/notes/{note_id}/delete")
async def note_delete(request: Request, note_id: int, session: DB) -> Response:
    await posted(request)
    note = await session.get(NotePreset, note_id)
    if note is None:
        raise HTTPException(404)
    await session.delete(note)
    audit(session, ACTOR, "note.deleted", f"note:{note_id}", {"title": note.title})
    await session.commit()
    return redirect("/admin/notes", "Note deleted", request)


# --- clients ------------------------------------------------------------------------


_CLIENT_TEXT = (
    "name",
    "eik",
    "vat_id",
    "address",
    "city",
    "country",
    "contact_name",
    "email",
    "phone",
    "iban",
    "notes",
)


def _parse_client(form: FormData, errors: f.FormErrors) -> dict[str, Any]:
    data: dict[str, Any] = {k: f.text(form, k) for k in _CLIENT_TEXT}
    if not data["name"]:
        errors["name"] = "Required"
    if data["email"] and not f.EMAIL.match(data["email"]):
        errors["email"] = "Not a valid email address"
    if data["iban"]:
        data["iban"] = f.iban(data["iban"], errors)
    chat = f.text(form, "telegram_chat_id")
    data["telegram_chat_id"] = None
    if chat:
        try:
            data["telegram_chat_id"] = int(chat)
        except ValueError:
            errors["telegram_chat_id"] = "Telegram chat ids are numbers, e.g. 123456789 or -100…"
    channels = [ch for ch in ("email", "telegram") if f.checkbox(form, f"channel_{ch}")]
    if "email" in channels and not data["email"]:
        errors["channel_email"] = "Add an email address to deliver by email"
    if "telegram" in channels and data["telegram_chat_id"] is None:
        errors["channel_telegram"] = "Add a Telegram chat id to deliver by Telegram"
    data["delivery_channels"] = channels
    company_raw = f.text(form, "company_id")
    data["company_id"] = int(company_raw) if company_raw else None
    data["archived"] = f.checkbox(form, "archived")
    return data


async def _companies(session: AsyncSession) -> list[Company]:
    return list((await session.scalars(select(Company).order_by(Company.code))).all())


@router.get("/clients")
async def clients_index(request: Request, session: DB) -> Response:
    items = (
        await session.scalars(
            select(Counterparty).order_by(Counterparty.archived, Counterparty.name)
        )
    ).all()
    return render(request, "clients.html", clients=items)


@router.get("/clients/new")
async def client_new(request: Request, session: DB) -> Response:
    return render(
        request,
        "client_form.html",
        client=Counterparty(delivery_channels=["email"]),
        values={},
        errors={},
        companies=await _companies(session),
    )


@router.post("/clients/new")
async def client_create(request: Request, session: DB) -> Response:
    form = await posted(request)
    errors = f.FormErrors()
    data = _parse_client(form, errors)
    if errors:
        return render(
            request,
            "client_form.html",
            422,
            client=Counterparty(),
            values=dict(form),
            errors=errors,
            companies=await _companies(session),
        )
    client = Counterparty(**data)
    session.add(client)
    await session.flush()
    audit(session, ACTOR, "client.created", f"client:{client.id}", {"name": client.name})
    await session.commit()
    return redirect(f"/admin/clients/{client.id}", "Client created", request)


@router.get("/clients/{client_id}")
async def client_edit(request: Request, client_id: int, session: DB) -> Response:
    client = await session.get(Counterparty, client_id)
    if client is None:
        raise HTTPException(404, "Client not found")
    return render(
        request,
        "client_form.html",
        client=client,
        values={},
        errors={},
        companies=await _companies(session),
    )


@router.post("/clients/{client_id}")
async def client_update(request: Request, client_id: int, session: DB) -> Response:
    client = await session.get(Counterparty, client_id)
    if client is None:
        raise HTTPException(404, "Client not found")
    form = await posted(request)
    errors = f.FormErrors()
    data = _parse_client(form, errors)
    if errors:
        return render(
            request,
            "client_form.html",
            422,
            client=client,
            values=dict(form),
            errors=errors,
            companies=await _companies(session),
        )
    changed = {k: str(v) for k, v in data.items() if getattr(client, k) != v}
    for k, v in data.items():
        setattr(client, k, v)
    audit(session, ACTOR, "client.updated", f"client:{client.id}", changed)
    await session.commit()
    return redirect(f"/admin/clients/{client.id}", "Client saved", request)


# --- team (Telegram whitelist) -----------------------------------------------------


async def _team_page(request: Request, session: DB, status: int = 200, **ctx: Any) -> HTMLResponse:  # noqa: ANN401
    users = (await session.scalars(select(AllowedUser).order_by(AllowedUser.id))).all()
    companies = (await session.scalars(select(Company).order_by(Company.code))).all()
    settings: Settings = request.app.state.settings
    ctx = {"edit": None, "values": {}, "errors": {}, **ctx}
    return render(
        request,
        "team.html",
        status,
        users=users,
        companies=companies,
        bootstrap=set(settings.bootstrap_approver_ids),
        **ctx,
    )


@router.get("/team")
async def team_index(request: Request, session: DB) -> Response:
    return await _team_page(request, session)


@router.post("/team")
async def team_save(request: Request, session: DB) -> Response:
    form = await posted(request)
    errors = f.FormErrors()
    user_id = f.text(form, "id")
    raw_tg = f.text(form, "telegram_id")
    tg_id = None
    try:
        tg_id = int(raw_tg)
        if tg_id <= 0:
            raise ValueError
    except ValueError:
        errors["telegram_id"] = "A Telegram user id is a positive number (ask @userinfobot)"
    role_raw = f.text(form, "role")
    if role_raw not in {r.value for r in UserRole}:
        errors["role"] = "Pick a role"
    known = {c.code for c in (await session.scalars(select(Company))).all()}
    codes = [c for c in form.getlist("companies") if isinstance(c, str) and c in known]
    if tg_id is not None and not errors.get("telegram_id"):
        clash = await session.scalar(
            select(AllowedUser.id).where(
                AllowedUser.telegram_id == tg_id,
                AllowedUser.id != (int(user_id) if user_id else 0),
            )
        )
        if clash:
            errors["telegram_id"] = "This Telegram user is already on the team"
    if errors:
        return await _team_page(
            request, session, 422, edit=user_id, values=dict(form), errors=errors
        )
    if user_id:
        user = await session.get(AllowedUser, int(user_id))
        if user is None:
            raise HTTPException(404)
        action = "user.updated"
    else:
        user = AllowedUser()
        session.add(user)
        action = "user.added"
    user.telegram_id, user.role = tg_id, UserRole(role_raw)
    user.display_name = f.text(form, "display_name") or None
    user.companies = sorted(codes)
    await session.flush()
    audit(
        session,
        ACTOR,
        action,
        f"tg:{tg_id}",
        {"role": role_raw, "companies": user.companies or "all", "name": user.display_name},
    )
    await session.commit()
    return redirect("/admin/team", "Team member saved", request)


@router.post("/team/{user_id}/delete")
async def team_delete(request: Request, user_id: int, session: DB) -> Response:
    await posted(request)
    user = await session.get(AllowedUser, user_id)
    if user is None:
        raise HTTPException(404)
    await session.delete(user)
    audit(session, ACTOR, "user.removed", f"tg:{user.telegram_id}", {"role": user.role.value})
    await session.commit()
    return redirect("/admin/team", "Removed from the team", request)


# --- audit log ----------------------------------------------------------------------

AUDIT_PAGE = 100


def _audit_query(params: Any) -> tuple[Any, dict[str, str]]:  # noqa: ANN401
    """Filters shared by the page and the CSV export."""
    from datetime import date, datetime, time, timedelta

    q = select(AuditLog)
    flt = {k: (params.get(k) or "").strip() for k in ("action", "entity", "actor", "from", "to")}
    if flt["action"]:
        q = q.where(AuditLog.action.contains(flt["action"]))
    if flt["entity"]:
        q = q.where(AuditLog.entity.contains(flt["entity"]))
    if flt["actor"]:
        q = q.where(AuditLog.actor.contains(flt["actor"]))
    for key, op in (("from", "ge"), ("to", "lt")):
        try:
            d = date.fromisoformat(flt[key]) if flt[key] else None
        except ValueError:
            flt[key] = ""
            continue
        if d is not None:
            bound = datetime.combine(d + timedelta(days=1 if op == "lt" else 0), time.min)
            q = q.where(AuditLog.ts >= bound if op == "ge" else AuditLog.ts < bound)
    return q, flt


@router.get("/audit")
async def audit_index(request: Request, session: DB) -> Response:
    q, flt = _audit_query(request.query_params)
    try:
        page = max(1, int(request.query_params.get("page") or 1))
    except ValueError:
        page = 1
    rows = (
        await session.scalars(
            q.order_by(AuditLog.id.desc()).offset((page - 1) * AUDIT_PAGE).limit(AUDIT_PAGE + 1)
        )
    ).all()
    actions = (
        await session.scalars(select(AuditLog.action).distinct().order_by(AuditLog.action))
    ).all()
    return render(
        request,
        "audit.html",
        rows=rows[:AUDIT_PAGE],
        more=len(rows) > AUDIT_PAGE,
        page=page,
        flt=flt,
        actions=actions,
        query=urlencode({k: v for k, v in flt.items() if v}),
    )


def _csv_cell(v: object) -> str:
    """Text for a CSV cell; spreadsheet formulas are neutralised (CSV injection)."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


@router.get("/audit.csv")
async def audit_csv(request: Request, session: DB) -> Response:
    import csv
    import io
    import json

    q, _ = _audit_query(request.query_params)
    rows = (await session.scalars(q.order_by(AuditLog.id).limit(100_000))).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "time_utc", "actor", "action", "entity", "payload"])
    for r in rows:
        w.writerow(
            [
                r.id,
                r.ts.strftime("%Y-%m-%d %H:%M:%S"),
                _csv_cell(r.actor),
                _csv_cell(r.action),
                _csv_cell(r.entity),
                _csv_cell(json.dumps(r.payload or {}, ensure_ascii=False, sort_keys=True)),
            ]
        )
    return Response(
        buf.getvalue().encode("utf-8-sig"),  # BOM: Excel opens UTF-8 correctly
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="audit-log.csv"'},
    )


# --- invoices -----------------------------------------------------------------------


@router.get("/invoices")
async def invoices_index(request: Request, session: DB) -> Response:
    rows = (
        await session.execute(
            select(Invoice, Company.code, Counterparty.name)
            .join(Company, Company.id == Invoice.issuer_id)
            .join(Counterparty, Counterparty.id == Invoice.counterparty_id)
            .order_by(Invoice.id.desc())
            .limit(200)
        )
    ).all()
    settings: Settings = request.app.state.settings
    return render(
        request, "invoices.html", rows=rows, link=lambda inv: inv_svc.public_url(settings, inv)
    )


@router.get("/invoices/{invoice_id}/pdf")
async def invoice_pdf(invoice_id: int, session: DB) -> Response:
    try:
        b = await inv_svc.load_bundle(session, invoice_id)
        pdf = (
            await inv_svc.stored_pdf(session, b)
            if b.invoice.number
            else await inv_svc.render_bundle_pdf(session, b)
        )
    except inv_svc.InvoiceError as e:
        raise HTTPException(404, str(e)) from e
    return Response(
        pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{inv_svc.pdf_filename(b.invoice)}"'},
    )


# --- bank API (Paynetics) ---------------------------------------------------------------


async def outbound_ip() -> str:
    """The public IP this server's outgoing requests come from (what Paynetics allow-lists)."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            return (await client.get("https://api.ipify.org")).text.strip()[:45]
    except httpx.HTTPError:
        return "unknown"


def _env_code(code: str) -> str:
    return code.upper().replace("-", "_")


@router.post("/companies/{company_id}/bank-test")
async def company_bank_test(
    request: Request, company_id: int, session: DB, settings: Cfg
) -> Response:
    company = await load_company(session, company_id)
    await posted(request)
    creds = settings.paynetics_credentials().get(_env_code(company.code))
    result: dict[str, Any] = {"outbound_ip": await outbound_ip()}
    if creds is None:
        result["error"] = "No credentials configured for this company."
    else:
        client = PayneticsClient(company.code, settings.paynetics_base_url, *creds)
        try:
            profile = await client.profile()
            accounts = await client.accounts()
            linked = {a.bank_account_ref for a in company.bank_accounts if a.bank_account_ref}
            result = {
                "outbound_ip": result["outbound_ip"],
                "profile": profile,
                "accounts": [(a, a.account in linked) for a in accounts],
            }
            audit(
                session,
                ACTOR,
                "bank.tested",
                f"company:{company.id}",
                {"ok": True, "accounts": len(accounts)},
            )
        except BankError as e:
            result["error"] = str(e)
            result["outbound_ip"] = await outbound_ip()
            audit(
                session,
                ACTOR,
                "bank.tested",
                f"company:{company.id}",
                {"ok": False, "error": str(e)[:200]},
            )
        finally:
            await client.aclose()
        await session.commit()
    return render(
        request, "company_form.html", company=company, values={}, errors={}, bank_test=result
    )


# --- rules ------------------------------------------------------------------------------


def _parse_rule(form: FormData, errors: f.FormErrors) -> dict[str, Any]:
    def company_id(key: str, required: bool = True) -> int | None:
        raw = f.text(form, key)
        if not raw or not raw.isdigit():
            if required:
                errors[key] = "Required"
            return None
        return int(raw)

    kind = f.text(form, "type") or "invoice"
    data: dict[str, Any] = {
        "type": RuleType.PAYOUT_ON_RECEIPT if kind == "payout" else RuleType.INVOICE_ON_RECEIPT,
        "trigger_company_id": company_id("trigger_company_id"),
        "enabled": f.checkbox(form, "enabled"),
    }
    if kind == "payout":
        data["mode"] = f.text(form, "mode") or "percent"
        if data["mode"] not in PAYOUT_MODES:
            errors["mode"] = "Choose how the payout is computed"
        raw = (f.text(form, "value") or "").replace(",", ".")
        try:
            data["value"] = Decimal(raw)
            if data["value"] < 0 or (data["mode"] == "percent" and data["value"] > 100):
                raise InvalidOperation
        except InvalidOperation:
            errors["value"] = "A number (0-100 for a percentage)"
        data["target_company_id"] = company_id("target_company_id", required=False)
        data["iban"] = f.text(form, "iban")
        data["name"] = f.text(form, "name")
        if data["iban"]:
            data["iban"] = f.iban(data["iban"], errors)
            if not data["name"]:
                errors["name"] = "Required with an IBAN"
        elif not data["target_company_id"]:
            errors["target_company_id"] = "Choose one of our companies or enter an IBAN"
        data["reference_template"] = f.text(form, "reference_template") or "Payout {date}"
        data["from_company_id"] = company_id("from_company_id", required=False)
    else:
        data["target_company_id"] = company_id("target_company_id")
        data["percent"] = f.percent(form, "percent", errors)
        data["service_description"] = f.text(form, "service_description")
        data["payment_terms_days"] = f.integer(form, "payment_terms_days", errors)
        data["include_internal"] = f.checkbox(form, "include_internal")
        if not data["service_description"]:
            errors["service_description"] = "Required: printed as the invoice line"
    if data["trigger_company_id"] and data["trigger_company_id"] == data["target_company_id"]:
        errors["target_company_id"] = "Must be a different company"
    return data


async def _save_rule(session: AsyncSession, rule: Rule, data: dict[str, Any]) -> None:
    trigger = await session.get(Company, data["trigger_company_id"])
    if trigger is None:
        raise HTTPException(400, "Unknown company")
    rule.type, rule.trigger_company_id = data["type"], trigger.id
    rule.enabled = data["enabled"]
    if data["type"] == RuleType.PAYOUT_ON_RECEIPT:
        # target_company_id is NOT NULL in the schema; with an external IBAN it points at the
        # paying company itself and is ignored by the engine.
        rule.target_company_id = data["target_company_id"] or trigger.id
        rule.params = {
            "mode": data["mode"],
            "value": str(data["value"]),
            "iban": data["iban"] if data["iban"] else None,
            "name": data["name"] if data["iban"] else None,
            "reference_template": data["reference_template"],
            "from_company_id": data["from_company_id"],
        }
        return
    if await session.get(Company, data["target_company_id"]) is None:
        raise HTTPException(400, "Unknown company")
    client = await counterparty_for_company(session, trigger)
    contract_id = (rule.params or {}).get("contract_id")
    contract = await session.get(Contract, contract_id) if contract_id else None
    if contract is None:
        contract = Contract(
            company_id=data["target_company_id"], counterparty_id=client.id, service_description=""
        )
        session.add(contract)
    contract.company_id, contract.counterparty_id = data["target_company_id"], client.id
    contract.service_description = data["service_description"]
    contract.fee_rule, contract.fee_value = FeeRuleType.PERCENT, data["percent"]
    contract.payment_terms_days = data["payment_terms_days"]
    await session.flush()
    rule.target_company_id = data["target_company_id"]
    rule.params = {
        "percent": str(data["percent"]),
        "contract_id": contract.id,
        "include_internal": data["include_internal"],
    }


async def _rule_form(
    request: Request,
    session: AsyncSession,
    rule: Rule,
    status: int = 200,
    values: dict[str, Any] | None = None,
    errors: dict[str, str] | None = None,
) -> Response:
    contract_id = (rule.params or {}).get("contract_id")
    contract = await session.get(Contract, contract_id) if contract_id else None
    runs = []
    if rule.id:
        runs = (
            await session.execute(
                select(RuleRun, BankTransfer, Invoice)
                .join(BankTransfer, BankTransfer.id == RuleRun.transfer_id)
                .outerjoin(Invoice, Invoice.id == RuleRun.invoice_id)
                .where(RuleRun.rule_id == rule.id)
                .order_by(RuleRun.id.desc())
                .limit(20)
            )
        ).all()
    return render(
        request,
        "rule_form.html",
        status,
        rule=rule,
        contract=contract,
        companies=await _companies(session),
        values=values or {},
        errors=errors or {},
        runs=runs,
    )


@router.get("/rules")
async def rules_index(request: Request, session: DB) -> Response:
    rules = (await session.scalars(select(Rule).order_by(Rule.id))).all()
    companies = {c.id: c for c in await _companies(session)}
    contracts = {c.id: c for c in (await session.scalars(select(Contract))).all()}
    return render(request, "rules.html", rules=rules, companies=companies, contracts=contracts)


@router.get("/rules/new")
async def rule_new(request: Request, session: DB) -> Response:
    kind = request.query_params.get("type")
    rule = (
        Rule(
            enabled=True,
            type=RuleType.PAYOUT_ON_RECEIPT,
            params={"mode": "percent", "value": "100", "reference_template": "Payout {date}"},
        )
        if kind == "payout"
        else Rule(enabled=True, type=RuleType.INVOICE_ON_RECEIPT, params={"percent": "1.5"})
    )
    return await _rule_form(request, session, rule)


@router.post("/rules/new")
async def rule_create(request: Request, session: DB) -> Response:
    form = await posted(request)
    errors = f.FormErrors()
    data = _parse_rule(form, errors)
    rule = Rule(params={}, type=data["type"])
    if errors:
        return await _rule_form(request, session, rule, 422, dict(form), errors)
    await _save_rule(session, rule, data)
    session.add(rule)
    await session.flush()
    audit(session, ACTOR, "rule.created", f"rule:{rule.id}", rule.params)
    await session.commit()
    return redirect(f"/admin/rules/{rule.id}", "Rule created", request)


@router.get("/rules/{rule_id}")
async def rule_edit(request: Request, rule_id: int, session: DB) -> Response:
    rule = await session.get(Rule, rule_id)
    if rule is None:
        raise HTTPException(404, "Rule not found")
    return await _rule_form(request, session, rule)


@router.post("/rules/{rule_id}")
async def rule_update(request: Request, rule_id: int, session: DB) -> Response:
    rule = await session.get(Rule, rule_id)
    if rule is None:
        raise HTTPException(404, "Rule not found")
    form = await posted(request)
    errors = f.FormErrors()
    data = _parse_rule(form, errors)
    if errors:
        return await _rule_form(request, session, rule, 422, dict(form), errors)
    await _save_rule(session, rule, data)
    audit(
        session, ACTOR, "rule.updated", f"rule:{rule.id}", {**rule.params, "enabled": rule.enabled}
    )
    await session.commit()
    return redirect(f"/admin/rules/{rule.id}", "Rule saved", request)
