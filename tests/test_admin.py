import re
from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select
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
    assert "Subtotal" not in live and "Total in words" not in live  # unchecked = hidden
    assert "Company ID 200000002" not in live  # company footer off

    full = client.get(
        f"/admin/templates/{tpl_id}/preview",
        params={
            "live": "1",
            "layout": "classic",
            "accent_color": "#0F766E",
            "font": "Inter",
            "title": "INVOICE",
            "subtitle": "Original",
            "logo_position": "left",
            "show_vat_breakdown": "on",
            "show_amount_in_words": "on",
            "show_company_footer": "on",
        },
    ).text
    assert "Subtotal" in full and "Original" in full
    assert "one thousand six hundred nineteen euro and sixty-four cents" in full
    assert "Company ID 200000002" in full and "Place of issue" in full

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


def test_clients_crud_and_channels(client: TestClient, sync_db: Session) -> None:
    from app.db.models import Counterparty

    token = login(client)
    base = {"name": "Client Ltd", "email": "ap@client.example", "channel_email": "on"}
    r = post(client, "/admin/clients/new", {**base, "email": "nope"}, token)
    assert r.status_code == 422 and "valid email" in r.text
    r = post(client, "/admin/clients/new", {**base, "channel_telegram": "on"}, token)
    assert r.status_code == 422 and "Telegram chat id to deliver" in r.text
    r = post(client, "/admin/clients/new", {**base, "telegram_chat_id": "abc"}, token)
    assert r.status_code == 422 and "chat ids are numbers" in r.text
    r = post(client, "/admin/clients/new", {**base, "iban": "BG80BNBG96611020345679"}, token)
    assert r.status_code == 422 and "checksum" in r.text

    r = post(
        client,
        "/admin/clients/new",
        {**base, "telegram_chat_id": "-100123", "channel_telegram": "on", "contact_name": "Ana"},
        token,
    )
    assert r.status_code == 303
    c = sync_db.scalar(select(Counterparty))
    assert c.delivery_channels == ["email", "telegram"] and c.telegram_chat_id == -100123
    page = client.get("/admin/clients").text
    assert "Client Ltd" in page and "Telegram -100123" in page

    r = post(
        client,
        f"/admin/clients/{c.id}",
        {
            "name": "Client Ltd",
            "email": "ap@client.example",
            "channel_email": "on",
            "archived": "on",
        },
        token,
    )
    assert r.status_code == 303
    sync_db.expire_all()
    c = sync_db.get(Counterparty, c.id)
    assert c.delivery_channels == ["email"] and c.telegram_chat_id is None and c.archived
    assert client.get("/admin/invoices").status_code == 200


def test_bank_api_panel(client: TestClient, sync_db: Session, monkeypatch) -> None:  # noqa: ANN001
    token = login(client)
    cid = company_id(sync_db, "X")
    page = client.get(f"/admin/companies/{cid}").text
    assert "PAYNETICS_X_ACCESS_TOKEN" in page and "webhooks/paynetics" in page
    from app.admin import routes as _routes

    async def no_ip() -> str:
        return "198.51.100.1"

    monkeypatch.setattr(_routes, "outbound_ip", no_ip)
    r = post(client, f"/admin/companies/{cid}/bank-test", {}, token)
    assert "No credentials configured" in r.text and "198.51.100.1" in r.text

    from app.admin import routes
    from app.bank.base import Balance

    class FakeClient:
        def __init__(self, *a) -> None:  # noqa: ANN002
            pass

        async def profile(self) -> dict:
            return {"name": "Company X Ltd"}

        async def accounts(self) -> list:
            return [
                Balance(
                    account="41411111111",
                    currency="EUR",
                    balance=Decimal("10"),
                    available=Decimal("10"),
                ),
                Balance(
                    account="41499999999",
                    currency="USD",
                    balance=Decimal("5"),
                    available=Decimal("5"),
                ),
            ]

        async def aclose(self) -> None:
            pass

    async def fake_ip() -> str:
        return "203.0.113.7"

    monkeypatch.setattr(routes, "outbound_ip", fake_ip)
    monkeypatch.setenv("PAYNETICS_X_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("PAYNETICS_X_SECRET_KEY", "sec")
    monkeypatch.setattr(routes, "PayneticsClient", FakeClient)
    html = post(client, f"/admin/companies/{cid}/bank-test", {}, token).text
    assert "Connected as <b>Company X Ltd</b>" in html and "203.0.113.7" in html
    assert "41499999999" in html and html.count('badge ok">yes') == 1

    class BrokenClient(FakeClient):
        async def accounts(self) -> list:
            raise AttributeError("'list' object has no attribute 'get'")

    monkeypatch.setattr(routes, "PayneticsClient", BrokenClient)
    r = post(client, f"/admin/companies/{cid}/bank-test", {}, token)
    assert r.status_code == 200 and "Unexpected response from Paynetics" in r.text


def test_rules_admin(client: TestClient, sync_db: Session) -> None:
    from app.db.models import Contract, Counterparty, Rule

    token = login(client)
    x, z = company_id(sync_db, "X"), company_id(sync_db, "Z")
    base = {
        "trigger_company_id": str(x),
        "target_company_id": str(z),
        "percent": "1.5",
        "payment_terms_days": "10",
        "service_description": "Marketing services",
        "enabled": "on",
    }
    r = post(client, "/admin/rules/new", {**base, "target_company_id": str(x)}, token)
    assert r.status_code == 422 and "different company" in r.text
    r = post(client, "/admin/rules/new", {**base, "service_description": ""}, token)
    assert r.status_code == 422 and "invoice line" in r.text
    r = post(client, "/admin/rules/new", base, token)
    assert r.status_code == 303
    rule = sync_db.scalar(select(Rule))
    contract = sync_db.get(Contract, rule.params["contract_id"])
    cp = sync_db.get(Counterparty, contract.counterparty_id)
    assert rule.params["percent"] == "1.50" and rule.enabled
    assert contract.company_id == z and cp.company_id == x and cp.name == "Company X Ltd"
    assert contract.service_description == "Marketing services"

    r = post(
        client,
        f"/admin/rules/{rule.id}",
        {**base, "percent": "2", "service_description": "Media buying"},
        token,
    )
    assert r.status_code == 303
    sync_db.expire_all()
    rule = sync_db.get(Rule, rule.id)
    assert rule.params["percent"] == "2.00" and rule.enabled is not False
    assert sync_db.scalar(select(func.count(Contract.id))) == 1  # contract updated, not duplicated
    assert sync_db.get(Contract, rule.params["contract_id"]).service_description == "Media buying"
    page = client.get("/admin/rules").text
    assert "Y − 2.00%" in page and "Media buying" in page


def test_audit_log_page_and_csv(client: TestClient, sync_db: Session) -> None:
    for url in ("/admin/audit", "/admin/audit.csv"):
        r = client.get(url)
        assert r.status_code == 303 and r.headers["location"] == "/admin/login", url
    login(client)
    from datetime import UTC, datetime

    sync_db.add_all(
        [
            AuditLog(
                actor="tg:1001",
                action="invoice.approved",
                entity="invoice:7",
                payload={"number": "0000000007"},
                ts=datetime(2026, 10, 20, 9, tzinfo=UTC),
            ),
            AuditLog(
                actor="system",
                action="instruction.executed",
                entity="instruction:3",
                payload={"to": '=HYPERLINK("x")'},
                ts=datetime(2026, 10, 21, 9, tzinfo=UTC),
            ),
        ]
    )
    sync_db.commit()
    page = client.get("/admin/audit").text
    assert "invoice.approved" in page and "instruction.executed" in page
    page = client.get("/admin/audit?action=invoice.&from=2026-10-20&to=2026-10-20").text
    assert "invoice:7" in page and "instruction:3" not in page
    assert client.get("/admin/audit?from=nonsense").status_code == 200

    r = client.get("/admin/audit.csv?entity=instruction")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    lines = r.content.decode("utf-8-sig").splitlines()
    assert lines[0] == "id,time_utc,actor,action,entity,payload"
    assert len(lines) == 2 and "2026-10-21 09:00:00" in lines[1]
    assert "instruction.executed" in lines[1]


def test_csv_cells_cannot_become_formulas() -> None:
    from app.admin.routes import _csv_cell

    got = [_csv_cell(v) for v in ("=1+1", "+3", "-2", "@SUM", "ok", None, 5)]
    assert got == ["'=1+1", "'+3", "'-2", "'@SUM", "ok", "", "5"]


def test_team_add_edit_remove(client: TestClient, sync_db: Session) -> None:
    from app.db.models import AllowedUser, UserRole

    assert client.get("/admin/team").status_code == 303
    token = login(client)
    page = client.get("/admin/team").text
    assert "1001" in page and "bootstrap" in page  # the test settings' bootstrap approver

    r = post(client, "/admin/team", {"telegram_id": "abc", "role": "approver"}, token)
    assert r.status_code == 422 and "positive number" in r.text
    r = post(client, "/admin/team", {"telegram_id": "1001", "role": "operator"}, token)
    assert r.status_code == 422 and "already on the team" in r.text

    r = client.post(
        "/admin/team",
        data={
            "csrf": token,
            "telegram_id": "2007",
            "role": "approver",
            "display_name": "Maria",
            "companies": ["Z", "NOPE"],
        },
    )
    assert r.status_code == 303
    u = sync_db.scalar(select(AllowedUser).where(AllowedUser.telegram_id == 2007))
    assert (u.role, u.display_name, u.companies) == (UserRole.APPROVER, "Maria", ["Z"])

    r = post(
        client, "/admin/team", {"id": str(u.id), "telegram_id": "2007", "role": "operator"}, token
    )
    assert r.status_code == 303
    sync_db.expire_all()
    u = sync_db.get(AllowedUser, u.id)
    assert (u.role, u.companies) == (UserRole.OPERATOR, [])

    uid = u.id
    assert post(client, f"/admin/team/{uid}/delete", {}, token).status_code == 303
    sync_db.expire_all()
    assert sync_db.get(AllowedUser, uid) is None
    actions = sync_db.scalars(
        select(AuditLog.action).where(AuditLog.action.like("user.%")).order_by(AuditLog.id)
    ).all()
    assert actions[-3:] == ["user.added", "user.updated", "user.removed"]


def test_finance_page_csv_and_history(client: TestClient) -> None:
    for url in ("/admin/finance", "/admin/finance/transfers.csv"):
        assert client.get(url).status_code == 303
    token = login(client)
    page = client.get("/admin/finance?preset=last_month")
    assert page.status_code == 200 and "Owed to us now" in page.text
    assert "Balances now" in page.text  # the seeded mock accounts
    r = client.get("/admin/finance/transfers.csv?from=2026-10-01&to=2026-10-31")
    assert r.status_code == 200 and r.content.decode("utf-8-sig").startswith("date,company")
    assert "transfers-20261001-20261031.csv" in r.headers["content-disposition"]
    r = post(client, "/admin/finance/history", {"since": "2026-01-01"}, token)
    assert r.status_code == 303
    assert "Bank history" in client.get("/admin/finance").text


def test_mailbox_panel_and_test(client: TestClient, sync_db: Session, monkeypatch) -> None:  # noqa: ANN001
    from app.inbox import mail

    token = login(client)
    cid = company_id(sync_db, "X")
    page = client.get(f"/admin/companies/{cid}").text
    assert "IMAP_X_HOST" in page and "Not set up." in page
    html = post(client, f"/admin/companies/{cid}/imap-test", {}, token).text
    assert "No IMAP settings" in html
    for k, v in {"HOST": "imap.example.test", "USER": "inv@x.test", "PASSWORD": "p"}.items():
        monkeypatch.setenv(f"IMAP_X_{k}", v)
    monkeypatch.setattr(mail, "check_login", lambda box: 12)
    html = post(client, f"/admin/companies/{cid}/imap-test", {}, token).text
    assert "Connected to <b>inv@x.test@imap.example.test/INBOX</b>: 12 messages" in html

    def refuse(box):  # noqa: ANN001, ANN202
        raise mail.MailError("Login or folder failed: bad credentials")

    monkeypatch.setattr(mail, "check_login", refuse)
    assert "bad credentials" in post(client, f"/admin/companies/{cid}/imap-test", {}, token).text
