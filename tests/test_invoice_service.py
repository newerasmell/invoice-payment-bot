import asyncio
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.models import AuditLog, Company, Counterparty, InvoiceStatus
from app.invoices import service as svc
from tests.conftest import CLIENT_DETAILS


async def _setup(db, code: str = "X"):  # noqa: ANN001, ANN202
    async with db.session() as s:
        issuer = await s.scalar(
            select(Company).options(selectinload(Company.bank_accounts)).where(Company.code == code)
        )
        client = Counterparty(
            name="Client Ltd",
            email="ap@client.example",
            delivery_channels=["email"],
            **CLIENT_DETAILS,
        )
        s.add(client)
        await s.commit()
        return issuer, client


async def _draft(db, settings, issuer, client, **kw):  # noqa: ANN001, ANN003, ANN202
    lines = kw.pop("lines", [svc.LineInput("Consulting", Decimal("1"), Decimal("1200"))])
    async with db.session() as s:
        inv = await svc.create_draft(s, settings, issuer=issuer, client=client, lines=lines, **kw)
        await s.commit()
        return inv.id


async def test_draft_totals_and_defaults(db, settings) -> None:
    issuer, client = await _setup(db)
    inv_id = await _draft(
        db,
        settings,
        issuer,
        client,
        lines=[
            svc.LineInput("Consulting", Decimal("1"), Decimal("1200")),
            svc.LineInput("Hosting", Decimal("3"), Decimal("49.90"), "month"),
        ],
    )
    async with db.session() as s:
        b = await svc.load_bundle(s, inv_id)
    inv = b.invoice
    assert inv.status == InvoiceStatus.DRAFT and inv.number is None
    assert (inv.amount_net, inv.amount_vat, inv.amount_gross) == (
        Decimal("1349.70"),
        Decimal("269.94"),
        Decimal("1619.64"),
    )
    assert inv.currency == "EUR" and inv.vat_rate == Decimal("20.00")
    assert inv.due_date == date.today() + timedelta(days=14)
    assert inv.bank_account_id is not None and inv.template_id == issuer.default_template_id


async def test_amounts_including_vat(db, settings) -> None:
    issuer, client = await _setup(db)
    inv_id = await _draft(
        db,
        settings,
        issuer,
        client,
        include_vat=True,
        lines=[svc.LineInput("Gross item", Decimal("1"), Decimal("1200"))],
    )
    async with db.session() as s:
        inv = (await svc.load_bundle(s, inv_id)).invoice
    assert inv.amount_net == Decimal("1000.00") and inv.amount_gross == Decimal("1200.00")


async def test_limit(db, settings) -> None:
    issuer, client = await _setup(db)
    with pytest.raises(svc.InvoiceError, match="above the limit"):
        await _draft(
            db,
            settings,
            issuer,
            client,
            lines=[svc.LineInput("Big", Decimal("1"), Decimal("60000"))],
        )


async def test_numbering_has_no_gaps(db, settings) -> None:
    issuer, client = await _setup(db)
    a = await _draft(db, settings, issuer, client)
    rejected = await _draft(db, settings, issuer, client)
    b = await _draft(db, settings, issuer, client)
    async with db.session() as s:
        await svc.reject(s, await svc.load_bundle(s, rejected), "tg:1")
        await s.commit()
    numbers = []
    for inv_id in (a, b):
        async with db.session() as s:
            bundle = await svc.load_bundle(s, inv_id)
            pdf = await svc.approve(s, settings, bundle, 1001)
            numbers.append(bundle.invoice.number)
            assert pdf.startswith(b"%PDF")
    assert numbers == ["0000000001", "0000000002"]
    async with db.session() as s:
        assert (await s.get(Company, issuer.id)).invoice_next_number == 3
        with pytest.raises(svc.InvoiceError, match="already approved"):
            await svc.approve(s, settings, await svc.load_bundle(s, a), 1001)
        with pytest.raises(svc.InvoiceError, match="already cancelled"):
            await svc.reject(s, await svc.load_bundle(s, rejected), "tg:1")


async def test_concurrent_approvals_get_distinct_numbers(db, settings) -> None:
    issuer, client = await _setup(db)
    ids = [await _draft(db, settings, issuer, client) for _ in range(4)]

    async def approve(inv_id: int) -> str:
        async with db.session() as s:
            b = await svc.load_bundle(s, inv_id)
            await svc.approve(s, settings, b, 1001)
            return b.invoice.number

    numbers = await asyncio.gather(*(approve(i) for i in ids))
    assert sorted(numbers) == [f"{n:010d}" for n in range(1, 5)]


async def test_approval_freezes_data_and_stores_pdf(db, settings) -> None:
    issuer, client = await _setup(db)
    inv_id = await _draft(db, settings, issuer, client)
    async with db.session() as s:
        b = await svc.load_bundle(s, inv_id)
        await svc.approve(s, settings, b, 1001)
    # Profile edits after approval must not change the issued invoice.
    async with db.session() as s:
        (await s.get(Company, issuer.id)).legal_name = "Renamed Ltd"
        (await s.get(Counterparty, client.id)).name = "Other Client"
        await s.commit()
    async with db.session() as s:
        b = await svc.load_bundle(s, inv_id)
        assert b.invoice.snapshot["supplier"]["name"] == "Company X Ltd"
        assert b.invoice.snapshot["customer"]["name"] == "Client Ltd"
        assert b.invoice.snapshot["bank"]["iban"] == "BG86PAYN96611000000001"
        assert len(b.invoice.public_token) >= 30
        assert svc.public_url(settings, b.invoice).startswith("https://bot.example.com/i/")
        stored = await svc.stored_pdf(s, b)
        assert stored.startswith(b"%PDF")
        log = await s.scalar(select(AuditLog).where(AuditLog.action == "invoice.approved"))
        assert log.payload["number"] == "0000000001"


async def test_failed_pdf_rolls_back_number(db, settings, monkeypatch) -> None:
    issuer, client = await _setup(db)
    inv_id = await _draft(db, settings, issuer, client)

    async def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("renderer crashed")

    monkeypatch.setattr(svc, "render_bundle_pdf", boom)
    async with db.session() as s:
        with pytest.raises(RuntimeError):
            await svc.approve(s, settings, await svc.load_bundle(s, inv_id), 1001)
    async with db.session() as s:
        assert (await s.get(Company, issuer.id)).invoice_next_number == 1
        assert (await svc.load_bundle(s, inv_id)).invoice.status == InvoiceStatus.DRAFT


async def test_update_draft(db, settings) -> None:
    issuer, client = await _setup(db)
    inv_id = await _draft(db, settings, issuer, client)
    async with db.session() as s:
        b = await svc.load_bundle(s, inv_id)
        await svc.update_draft(
            s,
            settings,
            b,
            issuer=b.issuer,
            client=b.client,
            lines=[svc.LineInput("Consulting", Decimal("2"), Decimal("750"))],
            currency="EUR",
            vat_rate=Decimal("0"),
            include_vat=False,
            due_in_days=30,
            notes="Reverse charge",
            actor="tg:1",
        )
        await s.commit()
    async with db.session() as s:
        b = await svc.load_bundle(s, inv_id)
        assert b.invoice.amount_gross == Decimal("1500.00") and b.invoice.amount_vat == 0
        assert len(b.invoice.lines) == 1 and b.invoice.notes == "Reverse charge"
        assert svc.draft_as_dict(b)["due_in_days"] == 30
