"""Client invoice requests: secret link form -> Telegram -> draft -> approval -> link back."""

from collections.abc import Iterator
from decimal import Decimal

import pytest
from aiogram.methods import EditMessageText, SendDocument, SendMessage
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.bot.invoice_flow import Inv
from app.bot.requests_flow import Req, notify_request
from app.config import Settings
from app.db.models import (
    AllowedUser,
    Company,
    Counterparty,
    Invoice,
    InvoiceRequest,
    InvoiceStatus,
    RequestStatus,
    UserRole,
)
from app.invoices import requests as reqs
from app.main import create_app
from tests.conftest import CLIENT_DETAILS, BotHarness

TOKEN = "t" * 32


@pytest.fixture
def web_settings(settings: Settings, tmp_path) -> Settings:
    return settings.model_copy(
        update={
            "telegram_bot_token": None,
            "admin_password": SecretStr("pw-for-tests-123"),
            "session_secret": SecretStr("s"),
            "uploads_dir": tmp_path / "uploads",
            "poll_enabled": False,
            "reminders_enabled": False,
        }
    )


@pytest.fixture
def web(web_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(web_settings), follow_redirects=False) as c:
        yield c


@pytest.fixture
def sdb(web_settings: Settings) -> Iterator[Session]:
    engine = create_engine(web_settings.database_url.replace("+aiosqlite", ""))
    with Session(engine) as s:
        yield s
    engine.dispose()


def _client(sdb: Session, **kw) -> Counterparty:  # noqa: ANN003
    c = Counterparty(name="Requesting Client Ltd", request_token=TOKEN, **CLIENT_DETAILS, **kw)
    sdb.add(c)
    sdb.commit()
    return c


def _company_id(sdb: Session, code: str) -> int:
    return sdb.scalar(select(Company.id).where(Company.code == code))


def test_link_must_exist_and_client_not_archived(web: TestClient, sdb: Session) -> None:
    assert web.get("/r/short").status_code == 404
    assert web.get("/r/" + "x" * 32).status_code == 404
    c = _client(sdb)
    r = web.get(f"/r/{TOKEN}")
    assert r.status_code == 200 and "Requesting Client Ltd" in r.text
    assert r.headers["x-robots-tag"] == "noindex, nofollow"
    assert "Company X Ltd" in r.text and "Company Z Ltd" in r.text
    c.request_companies = ["Z"]
    sdb.commit()
    assert "Company X Ltd" not in web.get(f"/r/{TOKEN}").text
    c.archived = True
    sdb.commit()
    assert web.get(f"/r/{TOKEN}").status_code == 404


def test_submit_validation_and_limit(web: TestClient, sdb: Session) -> None:
    _client(sdb)
    z = _company_id(sdb, "Z")
    r = web.post(f"/r/{TOKEN}", data={"company_id": "", "desc": [""], "price": [""]})
    assert r.status_code == 422 and "Choose who should invoice you" in r.text
    assert "Add at least one line" in r.text
    r = web.post(
        f"/r/{TOKEN}",
        data={"company_id": z, "currency": "EUR", "desc": ["Work"], "qty": ["1"], "price": ["x"]},
    )
    assert r.status_code == 422 and "must be numbers" in r.text
    ok = {
        "company_id": z,
        "currency": "EUR",
        "desc": ["Consulting, October", "Hosting", ""],
        "qty": ["2", "1", "1"],
        "price": ["600", "49,90", ""],
        "note": "PO 4411",
        "contact": "Ana, ana@example.com",
    }
    r = web.post(f"/r/{TOKEN}", data=ok)
    assert r.status_code == 303 and r.headers["location"] == f"/r/{TOKEN}?sent=1"
    req = sdb.scalar(select(InvoiceRequest))
    assert req.status == RequestStatus.NEW and req.note == "PO 4411"
    assert req.lines == [
        {"description": "Consulting, October", "quantity": "2", "unit_price": "600.00"},
        {"description": "Hosting", "quantity": "1", "unit_price": "49.90"},
    ]
    assert reqs.total(req.lines) == Decimal("1249.90")
    page = web.get(f"/r/{TOKEN}?sent=1").text
    assert "your request was received" in page and "In review" in page
    for _ in range(reqs.MAX_OPEN - 1):
        assert web.post(f"/r/{TOKEN}", data=ok).status_code == 303
    r = web.post(f"/r/{TOKEN}", data=ok)
    assert r.status_code == 422 and "requests waiting" in r.text


def test_admin_creates_rotates_and_disables_link(web: TestClient, sdb: Session) -> None:
    import re

    c = Counterparty(name="No Link Ltd", **CLIENT_DETAILS)
    sdb.add(c)
    sdb.commit()
    html = web.get("/admin/login").text
    token = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    web.post("/admin/login", data={"csrf": token, "password": "pw-for-tests-123"})
    html = web.get(f"/admin/clients/{c.id}").text
    assert "Create request link" in html
    token = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    url = f"/admin/clients/{c.id}/request-link"
    assert web.post(url + "?do=enable", data={"csrf": token}).status_code == 303
    sdb.expire_all()
    first = sdb.get(Counterparty, c.id).request_token
    assert first and len(first) >= 30
    assert f"/r/{first}" in web.get(f"/admin/clients/{c.id}").text
    web.post(url + "?do=companies", data={"csrf": token, "companies": ["Z", "BAD"]})
    web.post(url + "?do=rotate", data={"csrf": token})
    sdb.expire_all()
    got = sdb.get(Counterparty, c.id)
    assert got.request_companies == ["Z"] and got.request_token not in (None, first)
    assert web.get(f"/r/{first}").status_code == 404
    web.post(url + "?do=disable", data={"csrf": token})
    sdb.expire_all()
    assert sdb.get(Counterparty, c.id).request_token is None
    assert web.post(url + "?do=enable", data={}).status_code in (400, 403)  # CSRF


async def _request(db, *, client_kw=None) -> int:  # noqa: ANN001
    async with db.session() as s:
        z = await s.scalar(select(Company).where(Company.code == "Z"))
        c = Counterparty(name="Requesting Client Ltd", **{**CLIENT_DETAILS, **(client_kw or {})})
        s.add(c)
        await s.flush()
        r = InvoiceRequest(
            counterparty_id=c.id,
            company_id=z.id,
            currency="EUR",
            lines=[{"description": "Consulting", "quantity": "2", "unit_price": "600.00"}],
            note="PO 4411",
            status=RequestStatus.NEW,
        )
        s.add(r)
        await s.commit()
        return r.id


async def test_telegram_draft_then_approve(harness: BotHarness, db) -> None:
    async with db.session() as s:
        s.add(AllowedUser(telegram_id=2008, role=UserRole.OPERATOR, companies=["X"]))
        await s.commit()
    req_id = await _request(db)
    before = len(harness.session.requests)
    await notify_request(harness.bot, db, req_id)
    sent = [c for c in harness.calls_since(before) if isinstance(c, SendMessage)]
    assert [c.chat_id for c in sent] == [1001]  # not the X-only operator
    assert "New invoice request" in sent[0].text and "1,200.00 EUR" in sent[0].text
    assert "PO 4411" in sent[0].text

    calls = await harness.click(Req(action="dr", id=req_id).pack())
    assert any(isinstance(c, EditMessageText) and "draft created" in c.text for c in calls)
    [preview] = [c for c in calls if isinstance(c, SendDocument)]
    assert "From a client request" in preview.caption
    async with db.session() as s:
        req = await s.get(InvoiceRequest, req_id)
        inv = await s.get(Invoice, req.invoice_id)
        assert req.status == RequestStatus.DRAFTED and inv.status == InvoiceStatus.DRAFT
        assert inv.amount_net == Decimal("1200.00") and inv.notes == "PO 4411"
    calls = await harness.click(Req(action="dr", id=req_id).pack())
    assert any("already drafted" in (getattr(c, "text", "") or "") for c in calls)
    await harness.click(Inv(action="ap", id=inv.id).pack())
    async with db.session() as s:
        client = await s.get(Counterparty, req.counterparty_id)
        [(r, _, issued)] = await reqs.history(s, client)
        assert issued.number == "0000000001" and issued.public_token


async def test_decline_and_incomplete_client(harness: BotHarness, db) -> None:
    req_id = await _request(db, client_kw={"city": None})
    calls = await harness.click(Req(action="dr", id=req_id).pack())
    assert any("Complete the client" in (getattr(c, "text", "") or "") for c in calls)
    calls = await harness.click(Req(action="no", id=req_id).pack())
    assert any("Request declined" in (getattr(c, "text", "") or "") for c in calls)
    async with db.session() as s:
        assert (await s.get(InvoiceRequest, req_id)).status == RequestStatus.DECLINED
    calls = await harness.send_raw("/requests")
    assert any("No client invoice requests" in (getattr(c, "text", "") or "") for c in calls)
