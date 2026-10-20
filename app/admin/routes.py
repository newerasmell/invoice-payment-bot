import asyncio
import logging
import secrets
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any

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
from app.companies import default_bank_account
from app.config import Settings
from app.db.models import (
    Company,
    CompanyBankAccount,
    Counterparty,
    Invoice,
    InvoiceTemplate,
    NotePreset,
)
from app.invoices import service as inv_svc
from app.invoices.options import FONTS, LAYOUTS, TemplateOptions
from app.invoices.render import render_invoice_html
from app.invoices.view import sample_invoice
from app.redact import redact_iban

log = logging.getLogger(__name__)
ACTOR = "admin:web"
LOGO_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/svg+xml": ".svg"}
MAX_LOGO_BYTES = 1_000_000

STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["redact_iban"] = redact_iban
templates.env.globals["LAYOUTS"] = LAYOUTS
templates.env.globals["FONTS"] = FONTS

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
