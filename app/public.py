"""Public, unauthenticated invoice links: /i/<token> serves the issued PDF.

The token is 32 random URL-safe characters, set only when an invoice is approved, so drafts
are never reachable and links cannot be guessed or enumerated.
"""

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.db.models import Invoice, InvoiceStatus
from app.invoices import requests as reqs
from app.invoices import service as svc

log = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "public_templates")
_PRIVATE = {
    "Cache-Control": "private, no-store",
    "X-Robots-Tag": "noindex, nofollow",
    "Referrer-Policy": "no-referrer",
}

_VISIBLE = (InvoiceStatus.APPROVED, InvoiceStatus.SENT, InvoiceStatus.PAID)


@router.get("/i/{token}", include_in_schema=False)
async def public_invoice(token: str, request: Request) -> Response:
    if len(token) < 20:
        raise HTTPException(404)
    async with request.app.state.db.session() as s:
        inv_id = await s.scalar(select(Invoice.id).where(Invoice.public_token == token))
        if inv_id is None:
            raise HTTPException(404)
        b = await svc.load_bundle(s, inv_id)
        if b.invoice.status not in _VISIBLE:
            raise HTTPException(404)
        pdf = await svc.stored_pdf(s, b)
    return Response(
        pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{svc.pdf_filename(b.invoice)}"',
            "Cache-Control": "private, no-store",
            "X-Robots-Tag": "noindex, nofollow",
            "Referrer-Policy": "no-referrer",
        },
    )


# --- client invoice requests ------------------------------------------------------------


async def _request_page(request: Request, s, client, status: int = 200, **ctx) -> HTMLResponse:  # noqa: ANN001, ANN003
    options = await reqs.choices(s, client)
    ctx = {"values": {}, "rows": [], "errors": {}, "sent": False, **ctx}
    resp = templates.TemplateResponse(
        request,
        "request.html",
        {
            "client": client,
            "options": options,
            "all_currencies": sorted({c for o in options for c in o.currencies}),
            "history": await reqs.history(s, client),
            "total": reqs.total,
            "max_lines": reqs.MAX_LINES,
            "row_count": max(3, len(ctx["rows"])),
            **ctx,
        },
        status_code=status,
    )
    resp.headers.update(_PRIVATE)
    return resp


@router.get("/r/{token}", include_in_schema=False)
async def request_form(token: str, request: Request) -> Response:
    async with request.app.state.db.session() as s:
        client = await reqs.client_by_token(s, token)
        if client is None:
            raise HTTPException(404)
        return await _request_page(request, s, client, sent="sent" in request.query_params)


@router.post("/r/{token}", include_in_schema=False)
async def request_submit(token: str, request: Request) -> Response:
    form = await request.form(max_files=0, max_fields=200)
    fields: dict[str, list[str]] = {}
    for k, v in form.multi_items():
        if isinstance(v, str):
            fields.setdefault(k, []).append(v)
    async with request.app.state.db.session() as s:
        client = await reqs.client_by_token(s, token)
        if client is None:
            raise HTTPException(404)
        data, errors = reqs.parse(fields, await reqs.choices(s, client))
        if not errors:
            try:
                req = await reqs.create(s, client, data)
                await s.commit()
            except reqs.RequestError as e:
                errors = {"limit": str(e)}
        if errors:
            values = {k: v[0] for k, v in fields.items() if v}
            return await _request_page(
                request, s, client, 422, values=values, rows=data.lines, errors=errors
            )
        req_id = req.id
    bot = getattr(request.app.state, "bot", None)
    if bot is not None:
        from app.bot.requests_flow import notify_request  # the bot is optional

        try:
            await notify_request(bot, request.app.state.db, req_id)
        except Exception:  # the request is saved; /requests in the bot still lists it
            log.exception("could not notify about invoice request %s", req_id)
    return RedirectResponse(f"/r/{token}?sent=1", status_code=303, headers=_PRIVATE)
