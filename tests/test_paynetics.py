import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from app.bank import paynetics as pn
from app.bank.base import BankAuthError, BankError
from app.db.models import TransferDirection

TODAY = datetime.now(UTC).date()


def t(tid: str, **kw) -> dict:  # noqa: ANN003
    base = {
        "transferId": tid,
        "transferType": "incoming",
        "payer": "Client Ltd",
        "payerAccount": "BG80BNBG96611020345678",
        "beneficiary": "Company X Ltd",
        "beneficiaryAccount": "41411111111",
        "amount": "1200.00",
        "currency": "EUR",
        "paymentDetails": "INV 0000000001",
        "transferStatus": "processed",
        "created": f"{TODAY} 10:15:00",
        "updated": f"{TODAY} 10:16:00",
    }
    return {**base, **kw}


class FakePaynetics:
    """Responses shaped exactly like docs/openapi-paynetics.yaml."""

    def __init__(self, transfers_by_day: dict, fail_first: int = 0) -> None:
        self.by_day = transfers_by_day
        self.calls: list[httpx.Request] = []
        self.fail_first = fail_first

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        if req.headers.get("Access-Token") != "tok" or req.headers.get("Secret-Key") != "sec":
            return httpx.Response(401, json={"message": "Unauthenticated."})
        if self.fail_first:
            self.fail_first -= 1
            return httpx.Response(502, json={"message": "An unexpected error occurred"})
        path, q = req.url.path, req.url.params
        if path == "/v1/profile":
            return httpx.Response(
                200, json={"status": "success", "data": {"name": "Company X Ltd"}}
            )
        if path == "/v1/accounts":
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "account": [
                            {
                                "accountNumber": "41411111111",
                                "accountName": "Main",
                                "currency": "EUR",
                                "balance": "10500.50",
                                "availableBalance": "10400.00",
                            }
                        ]
                    },
                },
            )
        if path == "/v1/accounts/41411111111":
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "account": {
                            "accountNumber": "41411111111",
                            "accountName": "Main",
                            "currency": "EUR",
                            "balance": "10500.50",
                            "availableBalance": "10400.00",
                        }
                    },
                },
            )
        if path == "/v1/transfers":
            pages = self.by_day.get(q["date"])
            assert q["type"] == "all" and q["status"] == "all"
            if not pages:
                return httpx.Response(404, json={"message": "No account found"})
            page = int(q.get("page", 1))
            return httpx.Response(
                200,
                json={
                    "data": {
                        "status": "success",
                        "transfers": pages[page - 1],
                        "pagination": {"current_page": page, "last_page": len(pages)},
                    }
                },
            )
        if path.startswith("/v1/transfers/"):
            tid = path.rsplit("/", 1)[1]
            for pages in self.by_day.values():
                for page in pages:
                    for item in page:
                        if item["transferId"] == tid:
                            return httpx.Response(
                                200, json={"status": "success", "transfer-details": [item]}
                            )
            return httpx.Response(404, json={"status": "error"})
        return httpx.Response(404)


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(pn, "_BACKOFF", (0, 0, 0))


def client(fake: FakePaynetics, token: str = "tok") -> pn.PayneticsClient:
    return pn.PayneticsClient(
        "X", "https://api.paynetics.online/v1", token, "sec", transport=httpx.MockTransport(fake)
    )


async def test_headers_profile_accounts() -> None:
    fake = FakePaynetics({})
    c = client(fake)
    assert (await c.profile())["name"] == "Company X Ltd"
    [acc] = await c.accounts()
    assert (acc.account, acc.balance, acc.available) == (
        "41411111111",
        Decimal("10500.50"),
        Decimal("10400.00"),
    )
    assert fake.calls[0].url == "https://api.paynetics.online/v1/profile"


async def test_auth_error_and_retries() -> None:
    with pytest.raises(BankAuthError):
        await client(FakePaynetics({}), token="wrong").profile()
    fake = FakePaynetics({}, fail_first=2)
    assert (await client(fake).profile())["name"] == "Company X Ltd"
    assert len(fake.calls) == 3
    with pytest.raises(BankError, match="after retries"):
        await client(FakePaynetics({}, fail_first=9)).profile()


async def test_pagination_404_empty_and_detail() -> None:
    day = TODAY.isoformat()
    fake = FakePaynetics({day: [[t("A1")], [t("A2", amount="5.5")]]})
    c = client(fake)
    assert [x["transferId"] for x in await c.transfers_on(TODAY)] == ["A1", "A2"]
    assert await c.transfers_on(TODAY - timedelta(days=1)) == []  # 404 = empty
    assert (await c.transfer("A2"))["amount"] == "5.5"
    assert await c.transfer("NOPE") is None


async def test_provider_parses_and_dedupes_across_companies() -> None:
    day = TODAY.isoformat()
    shared = t("S1", transferType="", payerAccount="41422222222", payer="Company Z Ltd")
    fx = FakePaynetics({day: [[t("A1"), shared]]})
    fz = FakePaynetics(
        {
            day: [
                [
                    shared,
                    t(
                        "B1",
                        transferType="outgoing",
                        payer="Company Z Ltd",
                        payerAccount="41422222222",
                        beneficiaryAccount="BG11X",
                    ),
                ]
            ]
        }
    )
    cz = pn.PayneticsClient(
        "Z", "https://api.paynetics.online/v1", "tok", "sec", transport=httpx.MockTransport(fz)
    )
    prov = pn.PayneticsProvider(
        {"X": client(fx), "Z": cz}, {"X": {"41411111111"}, "Z": {"41422222222"}}
    )
    got = {
        x.external_id: x for x in await prov.list_transfers(datetime.now(UTC) - timedelta(hours=30))
    }
    # S1 (Z -> X) is kept once per company: incoming for X, outgoing for Z.
    assert set(got) == {"X:A1", "X:S1", "Z:S1", "Z:B1"}
    a1 = got["X:A1"]
    assert (a1.direction, a1.amount, a1.owner_code, a1.account) == (
        TransferDirection.INCOMING,
        Decimal("1200.00"),
        "X",
        "41411111111",
    )
    assert a1.booked_at and a1.booked_at.tzinfo is not None
    assert a1.bank_id == "A1"
    assert got["X:S1"].direction == TransferDirection.INCOMING  # inferred from X's account
    assert got["Z:S1"].direction == TransferDirection.OUTGOING
    assert got["Z:B1"].direction == TransferDirection.OUTGOING
    assert got["Z:B1"].account == "41422222222"
    only_out = await prov.list_transfers(
        datetime.now(UTC) - timedelta(hours=30), TransferDirection.OUTGOING
    )
    assert sorted(x.external_id for x in only_out) == ["Z:B1", "Z:S1"]
    assert (await prov.get_balance("41411111111")).balance == Decimal("10500.50")
    assert (await prov.get_transfer("B1")).owner_code == "Z"
    assert [v.owner_code for v in await prov.get_transfer_views("S1")] == ["X", "Z"]
    assert (await prov.get_transfer("Z:S1")).direction == TransferDirection.OUTGOING


def test_date_parsing() -> None:
    assert pn._parse_dt("2026-10-20 10:15:00").tzinfo is not None
    assert pn._parse_dt("2026-10-20T10:15:00Z").hour == 10
    assert pn._parse_dt("20.10.2026 10:15:00").day == 20
    assert pn._parse_dt("garbage") is None and pn._parse_dt(None) is None


def test_spec_file_agrees_with_client() -> None:
    """Guard against silent spec drift: the endpoints and enums we rely on still exist."""
    import yaml

    spec = yaml.safe_load(open("docs/openapi-paynetics.yaml"))
    paths = spec["paths"]
    assert {"/v1/profile", "/v1/transfers", "/v1/transfers/{transferId}"} <= set(paths)
    params = {p["name"]: p for p in paths["/v1/transfers"]["get"]["parameters"]}
    assert {"incoming", "outgoing", "all"} <= set(params["type"]["schema"]["enum"])
    assert "processed" in params["status"]["schema"]["enum"]
    props = spec["components"]["schemas"]["transfers"]["properties"]
    assert {"transferId", "amount", "paymentDetails", "transferStatus"} <= set(props)
    json.dumps(props)


async def test_cloudflare_challenge_is_explained() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={"content-type": "text/html; charset=UTF-8", "cf-mitigated": "challenge"},
            text="<!DOCTYPE html><html><head><title>Just a moment...</title>",
        )

    c = pn.PayneticsClient(
        "X", "https://api.paynetics.online/v1", "tok", "sec", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(pn.BankBlockedError, match="allow-list"):
        await c.profile()

    def html_500(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, headers={"content-type": "text/html"}, text="<html>x</html>")

    c = pn.PayneticsClient(
        "X",
        "https://api.paynetics.online/v1",
        "tok",
        "sec",
        transport=httpx.MockTransport(html_500),
    )
    with pytest.raises(BankError) as e:
        await c.profile()
    assert "<html>" not in str(e.value)
