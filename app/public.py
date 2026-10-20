"""Public, unauthenticated invoice links: /i/<token> serves the issued PDF.

The token is 32 random URL-safe characters, set only when an invoice is approved, so drafts
are never reachable and links cannot be guessed or enumerated.
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select

from app.db.models import Invoice, InvoiceStatus
from app.invoices import service as svc

router = APIRouter()

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
