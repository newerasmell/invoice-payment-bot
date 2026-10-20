import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.admin.forms import iban_is_valid
from app.config import Settings
from app.db.models import AuditLog, Company, CompanyBankAccount, InvoiceTemplate, NotePreset
from app.main import create_app

PASSWORD = "correct horse battery staple"
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360f8cf00000301010018dd8db40000000049454e44ae426082"
)


@pytest.fixture
def admin_settings(settings: Settings, tmp_path) -> Settings:
    return settings.model_copy(
        update={
            "telegram_bot_token": None,
            "admin_password": SecretStr(PASSWORD),
            "session_secret": SecretStr("test-secret"),
            "uploads_dir": tmp_path / "uploads",
        }
    )


@pytest.fixture
def client(admin_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(admin_settings), follow_redirects=False) as c:
        yield c


@pytest.fixture
def sync_db(admin_settings: Settings) -> Iterator[Session]:
    url = admin_settings.database_url.replace("+aiosqlite", "")
    engine = create_engine(url)
    with Session(engine) as s:
        yield s
    engine.dispose()


def csrf_of(client: TestClient, url: str) -> str:
    html = client.get(url).text
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, f"no csrf token on {url}"
    return m.group(1)


def login(client: TestClient) -> str:
    token = csrf_of(client, "/admin/login")
    r = client.post("/admin/login", data={"csrf": token, "password": PASSWORD})
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    return csrf_of(client, "/admin")  # the session (and its CSRF token) is rotated on login


def post(client: TestClient, url: str, data: dict, token: str, **kw):
    return client.post(url, data={"csrf": token, **data}, **kw)


def company_id(db: Session, code: str) -> int:
    db.expire_all()
    return db.scalar(select(Company.id).where(Company.code == code))


def test_requires_login(client: TestClient) -> None:
    for url in ("/admin", "/admin/companies/1", "/admin/templates/1/preview", "/admin/notes"):
        r = client.get(url)
        assert r.status_code == 303 and r.headers["location"] == "/admin/login", url


def test_wrong_password_and_csrf(client: TestClient) -> None:
    token = csrf_of(client, "/admin/login")
    r = client.post("/admin/login", data={"csrf": token, "password": "nope"})
    assert r.status_code == 401 and "Wrong password" in r.text
    r = client.post("/admin/login", data={"csrf": "forged", "password": PASSWORD})
    assert r.status_code == 400
    assert client.get("/admin").status_code == 303


def test_disabled_without_password(admin_settings: Settings) -> None:
    s = admin_settings.model_copy(update={"admin_password": None})
    with TestClient(create_app(s), follow_redirects=False) as c:
        assert "panel is disabled" in c.get("/admin/login").text
        assert c.get("/admin").status_code == 303


def test_dashboard_lists_seeded_companies(client: TestClient) -> None:
    login(client)
    html = client.get("/admin").text
    assert "Company X Ltd" in html and "Company Z Ltd" in html and "Company G Ltd" in html
    assert "BG**...0001" in html


def test_create_company_with_validation(client: TestClient, sync_db: Session) -> None:
    token = login(client)
    base = {
        "code": "q1",
        "name": "Q One",
        "legal_name": "Q One Ltd",
        "invoice_next_number": "1",
        "default_currency": "eur",
        "default_vat_rate": "20",
        "default_payment_terms_days": "30",
    }
    r = post(client, "/admin/companies/new", {**base, "code": "bad code!"}, token)
    assert r.status_code == 422 and "letters, digits" in r.text
    r = post(client, "/admin/companies/new", {**base, "code": "x"}, token)
    assert r.status_code == 422 and "already uses this code" in r.text
    r = post(client, "/admin/companies/new", {**base, "default_vat_rate": "120"}, token)
    assert r.status_code == 422 and "between 0 and 100" in r.text

    r = post(client, "/admin/companies/new", base, token)
    assert r.status_code == 303
    c = sync_db.scalar(select(Company).where(Company.code == "Q1"))
    assert c.legal_name == "Q One Ltd" and c.default_currency == "EUR"
    assert c.default_payment_terms_days == 30
    assert sync_db.scalar(select(AuditLog).where(AuditLog.action == "company.created"))


def test_edit_profile(client: TestClient, sync_db: Session) -> None:
    token = login(client)
    cid = company_id(sync_db, "X")
    page = client.get(f"/admin/companies/{cid}").text
    assert 'value="Company X Ltd"' in page
    r = post(
        client,
        f"/admin/companies/{cid}",
        {
            "code": "X",
            "name": "Company X",
            "legal_name": "Company X Holdings Ltd",
            "eik": "200000001",
            "vat_id": "BG200000001",
            "manager": "New Manager",
            "invoice_next_number": "57",
            "default_currency": "EUR",
            "default_vat_rate": "9",
            "default_payment_terms_days": "7",
        },
        token,
    )
    assert r.status_code == 303
    sync_db.expire_all()
    c = sync_db.get(Company, cid)
    assert c.legal_name == "Company X Holdings Ltd" and c.manager == "New Manager"
    assert c.invoice_next_number == 57 and str(c.default_vat_rate) == "9.00"
    log = sync_db.scalar(select(AuditLog).where(AuditLog.action == "company.updated"))
    assert log.payload["manager"] == "New Manager"


def test_bank_accounts(client: TestClient, sync_db: Session) -> None:
    token = login(client)
    cid = company_id(sync_db, "X")
    url = f"/admin/companies/{cid}/accounts/new"
    data = {
        "label": "EUR - Other bank",
        "currency": "EUR",
        "iban": "BG80 BNBG 9661 1020 3456 79",
        "bic": "BNBGBGSD",
        "is_default": "on",
    }
    r = post(client, url, data, token)
    assert r.status_code == 422 and "checksum" in r.text
    r = post(client, url, {**data, "iban": "bg80 bnbg 9661 1020 3456 78"}, token)
    assert r.status_code == 303
    sync_db.expire_all()
    accounts = sync_db.scalars(
        select(CompanyBankAccount)
        .where(CompanyBankAccount.company_id == cid)
        .order_by(CompanyBankAccount.id)
    ).all()
    assert [a.iban for a in accounts] == ["BG86PAYN96611000000001", "BG80BNBG96611020345678"]
    # The new default replaced the old default for EUR.
    assert [a.is_default for a in accounts] == [False, True]

    new_id = accounts[1].id
    r = post(client, f"/admin/companies/{cid}/accounts/{new_id}/delete", {}, token)
    assert r.status_code == 303
    sync_db.expire_all()
    assert sync_db.get(CompanyBankAccount, new_id) is None


def test_template_editor_and_preview(client: TestClient, sync_db: Session) -> None:
    token = login(client)
    cid = company_id(sync_db, "Z")
    tpl_id = sync_db.scalar(select(Company.default_template_id).where(Company.id == cid))

    preview = client.get(f"/admin/templates/{tpl_id}/preview").text
    assert "Company Z Ltd" in preview and "1,619.64" in preview and "#0F766E" in preview

    live = client.get(
        f"/admin/templates/{tpl_id}/preview",
        params={
            "live": "1",
            "layout": "modern",
            "accent_color": "#7C3AED",
            "font": "Roboto",
            "title": "TAX INVOICE",
            "logo_position": "left",
            "show_signature": "on",
        },
    ).text
    assert "#7C3AED" in live and "TAX INVOICE" in live and "Received by" in live
    assert "Taxable amount" not in live  # unchecked box = hidden

    r = post(
        client,
        f"/admin/templates/{tpl_id}",
        {
            "name": "Classic",
            "layout": "classic",
            "accent_color": "red",
            "font": "Inter",
        },
        token,
    )
    assert r.status_code == 422 and "accent colour" in r.text

    r = post(
        client,
        f"/admin/templates/{tpl_id}",
        {
            "name": "Bold",
            "layout": "minimal",
            "accent_color": "#b91c1c",
            "font": "Source Serif",
            "title": "INVOICE",
            "logo_position": "right",
            "show_vat_breakdown": "on",
            "show_bank_details": "on",
            "footer_note": "Registered in Sofia",
        },
        token,
    )
    assert r.status_code == 303
    sync_db.expire_all()
    tpl = sync_db.get(InvoiceTemplate, tpl_id)
    assert tpl.name == "Bold" and tpl.layout == "minimal"
    assert tpl.options["accent_color"] == "#B91C1C" and tpl.options["show_due_date"] is False
    saved = client.get(f"/admin/templates/{tpl_id}/preview").text
    assert "Registered in Sofia" in saved and "Source Serif" in saved


def test_template_variants(client: TestClient, sync_db: Session) -> None:
    token = login(client)
    cid = company_id(sync_db, "X")
    first = sync_db.scalar(select(Company.default_template_id).where(Company.id == cid))

    r = post(client, f"/admin/templates/{first}/duplicate", {}, token)
    copy_id = int(r.headers["location"].rsplit("/", 1)[1])
    r = post(client, f"/admin/companies/{cid}/templates/new", {"name": "Export"}, token)
    new_id = int(r.headers["location"].rsplit("/", 1)[1])
    sync_db.expire_all()
    names = sorted(
        t.name
        for t in sync_db.scalars(select(InvoiceTemplate).where(InvoiceTemplate.company_id == cid))
    )
    assert names == ["Classic", "Classic (copy)", "Export"]

    post(client, f"/admin/templates/{new_id}/default", {}, token)
    sync_db.expire_all()
    assert sync_db.get(Company, cid).default_template_id == new_id
    # Archiving the default falls back to another live template.
    post(client, f"/admin/templates/{new_id}/archive", {}, token)
    sync_db.expire_all()
    assert sync_db.get(InvoiceTemplate, new_id).archived
    assert sync_db.get(Company, cid).default_template_id in (first, copy_id)

    page = client.get(f"/admin/companies/{cid}").text
    assert page.count("/preview") >= 3  # thumbnails


def test_logo_upload(client: TestClient, sync_db: Session) -> None:
    token = login(client)
    cid = company_id(sync_db, "X")
    r = post(
        client,
        f"/admin/companies/{cid}/logo",
        {},
        token,
        files={"logo": ("logo.txt", b"hello", "text/plain")},
    )
    assert r.status_code == 303
    assert "PNG, JPG or SVG" in client.get(f"/admin/companies/{cid}").text
    r = post(
        client,
        f"/admin/companies/{cid}/logo",
        {},
        token,
        files={"logo": ("logo.png", PNG_1PX, "image/png")},
    )
    assert r.status_code == 303
    r = client.get(f"/admin/companies/{cid}/logo")
    assert r.status_code == 200 and r.content == PNG_1PX
    tpl_id = sync_db.scalar(select(Company.default_template_id).where(Company.id == cid))
    assert f"/admin/companies/{cid}/logo" in client.get(f"/admin/templates/{tpl_id}/preview").text


def test_note_presets(client: TestClient, sync_db: Session) -> None:
    token = login(client)
    assert post(client, "/admin/notes", {"title": "", "text": ""}, token).status_code == 422
    r = post(
        client,
        "/admin/notes",
        {"title": "Reverse charge", "text": "VAT reverse charge applies."},
        token,
    )
    assert r.status_code == 303
    note = sync_db.scalar(select(NotePreset))
    assert note.company_id is None
    post(
        client,
        "/admin/notes",
        {
            "id": str(note.id),
            "title": "Reverse charge",
            "text": "Updated",
            "company_id": str(company_id(sync_db, "Z")),
        },
        token,
    )
    sync_db.expire_all()
    assert sync_db.get(NotePreset, note.id).text == "Updated"
    assert "Updated" in client.get("/admin/notes").text
    post(client, f"/admin/notes/{note.id}/delete", {}, token)
    sync_db.expire_all()
    assert sync_db.scalar(select(NotePreset)) is None


def test_logout(client: TestClient) -> None:
    token = login(client)
    assert client.get("/admin").status_code == 200
    post(client, "/admin/logout", {}, token)
    assert client.get("/admin").status_code == 303


def test_iban_checksum() -> None:
    assert iban_is_valid("BG80BNBG96611020345678")
    assert iban_is_valid("DE89370400440532013000")
    assert not iban_is_valid("BG80BNBG96611020345679")
    assert not iban_is_valid("not an iban")
