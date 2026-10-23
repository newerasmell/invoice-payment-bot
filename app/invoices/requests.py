"""Client invoice requests (public form /r/<token>).

A client with a secret link asks to be invoiced: which of our companies, currency, lines, a
note. Nothing is created from it automatically: an internal user turns it into a DRAFT (or
declines it) in Telegram, and the draft is approved like any other.
"""

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.audit import audit
from app.config import Settings
from app.db.models import (
    Company,
    Counterparty,
    Invoice,
    InvoiceRequest,
    RequestStatus,
)
from app.invoices import completeness as comp
from app.invoices import service as svc
from app.money import to_money

MAX_LINES = 20
MAX_OPEN = 5  # NEW requests per client at a time (the link is secret, but still)
MAX_TEXT = 300


class RequestError(Exception):
    """A user-facing reason (shown to the client or the agent)."""


@dataclass
class CompanyChoice:
    company: Company
    currencies: list[str]


@dataclass
class RequestInput:
    company_id: int | None = None
    currency: str = ""
    lines: list[dict[str, str]] = field(default_factory=list)
    note: str = ""
    contact: str = ""


def new_token() -> str:
    return secrets.token_urlsafe(24)


async def client_by_token(session: AsyncSession, token: str) -> Counterparty | None:
    if len(token) < 20:
        return None
    client = await session.scalar(select(Counterparty).where(Counterparty.request_token == token))
    return None if client is None or client.archived else client


async def choices(session: AsyncSession, client: Counterparty) -> list[CompanyChoice]:
    """Our companies this client may ask for, each with the currencies it has accounts in."""
    allowed = {c.upper() for c in client.request_companies or []}
    rows = (
        await session.scalars(
            select(Company).options(selectinload(Company.bank_accounts)).order_by(Company.name)
        )
    ).all()
    out = []
    for c in rows:
        if allowed and c.code.upper() not in allowed:
            continue
        if client.company_id == c.id:  # never ask a company to invoice itself
            continue
        currencies = sorted({a.currency for a in c.bank_accounts})
        if currencies:
            out.append(CompanyChoice(c, currencies))
    return out


def parse(form: dict[str, list[str]], options: list[CompanyChoice]) -> tuple[RequestInput, dict]:
    """Validate the posted form; `form` maps each field to all its values (line arrays)."""
    errors: dict[str, str] = {}
    one = {k: (v[0] if v else "").strip() for k, v in form.items()}
    data = RequestInput(note=one.get("note", "")[:2000], contact=one.get("contact", "")[:255])
    by_id = {str(o.company.id): o for o in options}
    pick = by_id.get(one.get("company_id", ""))
    if pick is None:
        errors["company_id"] = "Choose who should invoice you"
    else:
        data.company_id = pick.company.id
        if one.get("currency") not in pick.currencies:
            errors["currency"] = "Choose a currency"
        else:
            data.currency = one["currency"]
    descs, qtys, prices = (form.get(k, []) for k in ("desc", "qty", "price"))
    for i, desc in enumerate(descs[:MAX_LINES]):
        desc = desc.strip()[:MAX_TEXT]
        qty_raw = (qtys[i] if i < len(qtys) else "").strip() or "1"
        price_raw = (prices[i] if i < len(prices) else "").strip()
        if not desc and not price_raw:
            continue  # empty row
        try:
            qty = Decimal(qty_raw.replace(",", "."))
            price = to_money(Decimal(price_raw.replace(" ", "").replace(",", ".")))
        except InvalidOperation:
            errors[f"line{i}"] = f"Line {i + 1}: quantity and price must be numbers"
            continue
        if not desc or qty <= 0 or price <= 0 or qty > 100000 or price > Decimal("10000000"):
            errors[f"line{i}"] = f"Line {i + 1}: add a description, a quantity and a price"
            continue
        data.lines.append({"description": desc, "quantity": str(qty), "unit_price": str(price)})
    if not data.lines and not any(k.startswith("line") for k in errors):
        errors["lines"] = "Add at least one line"
    return data, errors


def total(lines: list[dict[str, str]]) -> Decimal:
    return to_money(
        sum((Decimal(li["quantity"]) * Decimal(li["unit_price"]) for li in lines), Decimal(0))
    )


async def create(session: AsyncSession, client: Counterparty, data: RequestInput) -> InvoiceRequest:
    open_count = await session.scalar(
        select(func.count(InvoiceRequest.id)).where(
            InvoiceRequest.counterparty_id == client.id,
            InvoiceRequest.status == RequestStatus.NEW,
        )
    )
    if (open_count or 0) >= MAX_OPEN:
        raise RequestError(
            f"You already have {MAX_OPEN} requests waiting. Please wait until they are handled."
        )
    assert data.company_id is not None
    req = InvoiceRequest(
        counterparty_id=client.id,
        company_id=data.company_id,
        currency=data.currency,
        lines=data.lines,
        note=data.note or None,
        contact=data.contact or None,
        status=RequestStatus.NEW,
    )
    session.add(req)
    await session.flush()
    audit(
        session,
        f"client:{client.id}",
        "request.received",
        f"request:{req.id}",
        {"company_id": data.company_id, "total": str(total(data.lines)), "currency": data.currency},
    )
    return req


async def history(session: AsyncSession, client: Counterparty) -> list[tuple]:
    """The client's requests, newest first, with the invoice when one was issued."""
    return list(
        (
            await session.execute(
                select(InvoiceRequest, Company, Invoice)
                .join(Company, Company.id == InvoiceRequest.company_id)
                .outerjoin(Invoice, Invoice.id == InvoiceRequest.invoice_id)
                .where(InvoiceRequest.counterparty_id == client.id)
                .order_by(InvoiceRequest.id.desc())
                .limit(20)
            )
        ).all()
    )


async def to_draft(
    session: AsyncSession, settings: Settings, req: InvoiceRequest, actor_tg: int
) -> Invoice:
    if req.status != RequestStatus.NEW:
        raise RequestError(f"Request #{req.id} is already {req.status.value.lower()}")
    issuer = await session.scalar(
        select(Company)
        .options(selectinload(Company.bank_accounts))
        .where(Company.id == req.company_id)
    )
    client = await session.get(Counterparty, req.counterparty_id)
    assert issuer is not None and client is not None
    gaps = comp.missing_issuer(issuer, req.currency)
    if gaps:
        raise RequestError(
            f"{issuer.code} is missing {comp.labels(gaps)}. Fix it in the admin panel first."
        )
    gaps = comp.missing_client(client)
    if gaps:
        raise RequestError(
            f"The client {client.name} is missing {comp.labels(gaps)}. "
            "Complete the client in the admin panel first."
        )
    inv = await svc.create_draft(
        session,
        settings,
        issuer=issuer,
        client=client,
        lines=[
            svc.LineInput(li["description"], Decimal(li["quantity"]), Decimal(li["unit_price"]))
            for li in req.lines
        ],
        currency=req.currency,
        notes=req.note,
        created_by=actor_tg,
    )
    req.status, req.invoice_id = RequestStatus.DRAFTED, inv.id
    req.decided_by, req.decided_at = actor_tg, datetime.now(UTC)
    audit(session, f"tg:{actor_tg}", "request.drafted", f"request:{req.id}", {"invoice": inv.id})
    return inv


async def decline(
    session: AsyncSession, req: InvoiceRequest, actor_tg: int, reason: str = ""
) -> None:
    if req.status != RequestStatus.NEW:
        raise RequestError(f"Request #{req.id} is already {req.status.value.lower()}")
    req.status = RequestStatus.DECLINED
    req.decided_by, req.decided_at = actor_tg, datetime.now(UTC)
    req.decline_reason = reason or None
    audit(session, f"tg:{actor_tg}", "request.declined", f"request:{req.id}", {"reason": reason})
