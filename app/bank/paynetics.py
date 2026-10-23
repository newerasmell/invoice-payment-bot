"""Read-only Paynetics client, written against docs/openapi-paynetics.yaml (v1.2.0).

Each of our companies has its own Paynetics account and API token, so there is one client per
company. Nothing here can move money: the API has no endpoint for it.
"""

import asyncio
import logging
import re
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from app.bank.base import Balance, BankAuthError, BankError, NotSupported, Transfer
from app.db.models import PaymentInstruction, TransferDirection
from app.money import InvalidAmount, to_money
from app.redact import redact_iban

log = logging.getLogger(__name__)

_RETRY_STATUS = {429, 500, 502, 503, 504}
_BACKOFF = (0.5, 1.5, 4.0)
_MAX_PAGES = 50
_DIRECTIONS = {d.value: d for d in TransferDirection}


class BankBlockedError(BankError):
    """A proxy/WAF (Cloudflare) in front of the API refused this server."""


def _is_cloudflare_challenge(r: httpx.Response) -> bool:
    ctype = r.headers.get("content-type", "")
    return "text/html" in ctype and (
        "cf-mitigated" in r.headers or "Just a moment" in r.text[:2000]
    )


def _parse_dt(value: Any) -> datetime | None:
    """The spec does not document the date format; accept ISO and 'YYYY-MM-DD HH:MM:SS'."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip().replace("Z", "+00:00")
    for candidate in (v, v.replace(" ", "T", 1)):
        try:
            dt = datetime.fromisoformat(candidate)
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            continue
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(v, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _norm(acc: str | None) -> str:
    return re.sub(r"\s+", "", acc or "").upper()


def parse_transfer(raw: dict[str, Any], owner_code: str, our_accounts: set[str]) -> Transfer:
    our = {_norm(a) for a in our_accounts if a}
    payer_acc, benef_acc = raw.get("payerAccount"), raw.get("beneficiaryAccount")
    kind = str(raw.get("transferType") or "").strip().lower()
    direction = _DIRECTIONS.get(kind)
    if direction is None:  # undocumented values: infer from which side is ours
        if _norm(benef_acc) in our and _norm(payer_acc) not in our:
            direction = TransferDirection.INCOMING
        elif _norm(payer_acc) in our and _norm(benef_acc) not in our:
            direction = TransferDirection.OUTGOING
        else:
            direction = TransferDirection.INTERNAL
    account = benef_acc if direction == TransferDirection.INCOMING else payer_acc
    try:
        amount = to_money(str(raw.get("amount", "0")))
    except InvalidAmount:
        amount = to_money("0")
    return Transfer(
        # A transfer between two of our companies appears in both accounts, once as outgoing
        # and once as incoming; each company's view is kept as its own record.
        external_id=f"{owner_code}:{raw.get('transferId')}",
        bank_id=str(raw.get("transferId")),
        direction=direction,
        status=str(raw.get("transferStatus") or "").strip().lower() or "unknown",
        amount=amount,
        currency=str(raw.get("currency") or "").upper() or "EUR",
        payer_name=raw.get("payer"),
        payer_account=payer_acc,
        beneficiary_name=raw.get("beneficiary"),
        beneficiary_account=benef_acc,
        reference=raw.get("paymentDetails") or None,
        booked_at=_parse_dt(raw.get("updated")) or _parse_dt(raw.get("created")),
        account=account,
        owner_code=owner_code,
        raw=raw,
    )


class PayneticsClient:
    def __init__(
        self,
        code: str,
        base_url: str,
        access_token: str,
        secret_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.code = code
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "Access-Token": access_token,
                "Secret-Key": secret_key,
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(20.0, connect=10.0),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """GET with retries on 429/5xx/network. Returns None on 404 (the API's 'empty')."""
        last: Exception | None = None
        for attempt in range(len(_BACKOFF) + 1):
            try:
                r = await self._http.get(path, params=params)
            except httpx.TransportError as e:
                last = e
            else:
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 404:
                    return None
                if r.status_code == 401:
                    raise BankAuthError(f"Paynetics rejected the {self.code} credentials (401)")
                if r.status_code == 403 and _is_cloudflare_challenge(r):
                    raise BankBlockedError(
                        "Blocked by Cloudflare in front of the Paynetics API (403 challenge). "
                        "Ask Paynetics support to allow-list this server's outbound IP addresses."
                    )
                if r.status_code not in _RETRY_STATUS:
                    body = r.text[:200] if "json" in r.headers.get("content-type", "") else ""
                    raise BankError(f"Paynetics {path} returned {r.status_code} {body}".strip())
                last = BankError(f"Paynetics {path} returned {r.status_code}")
                retry_after = r.headers.get("retry-after")
                if retry_after and retry_after.isdigit() and attempt < len(_BACKOFF):
                    await asyncio.sleep(min(int(retry_after), 30))
                    continue
            if attempt < len(_BACKOFF):
                await asyncio.sleep(_BACKOFF[attempt])
        raise BankError(f"Paynetics {path} failed after retries: {last}")

    async def profile(self) -> dict[str, Any]:
        body = await self._get("/profile")
        data = body.get("data") if isinstance(body, dict) else body
        if isinstance(data, list):  # tolerate a list: take the first profile
            data = next((d for d in data if isinstance(d, dict)), {})
        return data if isinstance(data, dict) else {}

    async def accounts(self) -> list[Balance]:
        return [self._balance(a) for a in _account_items(await self._get("/accounts"))]

    async def balance(self, account: str) -> Balance | None:
        body = await self._get(f"/accounts/{account}")
        if body is None:
            return None
        acc = ((body.get("data") or {}).get("account")) or {}
        if isinstance(acc, list):
            acc = next((a for a in acc if _norm(a.get("accountNumber")) == _norm(account)), {})
        return self._balance(acc) if acc else None

    @staticmethod
    def _balance(a: dict[str, Any]) -> Balance:
        # Amounts may come as "1,234.56" or numbers; to_money normalises both.
        bal = to_money(str(a.get("balance") or "0"))
        avail = a.get("availableBalance")
        return Balance(
            account=str(a.get("accountNumber") or ""),
            name=a.get("accountName"),
            currency=str(a.get("currency") or "EUR").upper(),
            balance=bal,
            available=to_money(str(avail)) if avail not in (None, "") else bal,
        )

    async def transfers_on(self, day: date) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while page <= _MAX_PAGES:
            body = await self._get(
                "/transfers",
                {"type": "all", "status": "all", "date": day.isoformat(), "page": page},
            )
            data = (body or {}).get("data") or {}
            out += [t for t in data.get("transfers") or [] if isinstance(t, dict)]
            last_page = int((data.get("pagination") or {}).get("last_page") or 1)
            if page >= last_page:
                break
            page += 1
        return out

    async def transfer(self, transfer_id: str) -> dict[str, Any] | None:
        body = await self._get(f"/transfers/{transfer_id}")
        details = (body or {}).get("transfer-details") or []
        return details[0] if details else None


class PayneticsProvider:
    """BankProvider over several per-company Paynetics accounts."""

    name = "paynetics"

    def __init__(
        self,
        clients: dict[str, PayneticsClient],
        accounts_by_code: dict[str, set[str]] | None = None,
    ) -> None:
        self.clients = clients
        # Our account numbers/IBANs per company code; refreshed from the DB before each sync.
        self.accounts_by_code: dict[str, set[str]] = accounts_by_code or {}

    def _client_for_account(self, account: str) -> PayneticsClient | None:
        key = _norm(account)
        for code, accs in self.accounts_by_code.items():
            if key in {_norm(a) for a in accs} and code in self.clients:
                return self.clients[code]
        return None

    async def get_balance(self, account: str) -> Balance:
        clients: Iterable[PayneticsClient] = (
            [c] if (c := self._client_for_account(account)) else self.clients.values()
        )
        for client in clients:
            bal = await client.balance(account)
            if bal is not None:
                return bal
        raise BankError(f"Account {redact_iban(account)} was not found at Paynetics")

    async def list_transfers(
        self, since: datetime, direction: TransferDirection | None = None
    ) -> list[Transfer]:
        days: list[date] = []
        d, today = since.astimezone(UTC).date(), datetime.now(UTC).date()
        while d <= today:
            days.append(d)
            d += timedelta(days=1)
        seen: dict[str, Transfer] = {}
        for code, client in self.clients.items():
            ours = self.accounts_by_code.get(code, set())
            for day in days:
                for raw in await client.transfers_on(day):
                    if not raw.get("transferId"):
                        continue
                    t = parse_transfer(raw, code, ours)
                    if direction and t.direction != direction:
                        continue
                    if t.booked_at and t.booked_at < since:
                        continue
                    seen.setdefault(t.external_id, t)
        return list(seen.values())

    async def get_transfer_views(self, transfer_id: str) -> list[Transfer]:
        """Every company's view of a bank transfer id (two for transfers between us)."""
        views = []
        for code, client in self.clients.items():
            raw = await client.transfer(transfer_id)
            if raw:
                views.append(parse_transfer(raw, code, self.accounts_by_code.get(code, set())))
        return views

    async def get_transfer(self, transfer_id: str) -> Transfer:
        bank_id = transfer_id.split(":", 1)[-1]
        code = transfer_id.split(":", 1)[0] if ":" in transfer_id else None
        for view in await self.get_transfer_views(bank_id):
            if code is None or view.owner_code == code:
                return view
        raise BankError(f"Transfer {transfer_id} not found at Paynetics")

    async def get_statement(
        self, account: str, date_from: datetime, date_to: datetime
    ) -> list[Transfer]:
        raise NotSupported("statements are not used yet")

    async def create_transfer(self, instr: PaymentInstruction) -> None:
        raise NotSupported("Paynetics has no API for creating transfers; execute it manually")

    async def aclose(self) -> None:
        for c in self.clients.values():
            await c.aclose()


def _account_items(body: Any) -> list[dict[str, Any]]:  # noqa: ANN401
    """Account objects from any of the shapes seen for "all accounts": the spec's
    {"data": {"account": {...}}}, a list under data.account / data.accounts, or data itself
    being a list."""
    data = body.get("data", body) if isinstance(body, dict) else body
    if isinstance(data, dict):
        data = (
            data.get("account") or data.get("accounts") or (data if "accountNumber" in data else [])
        )
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    return [a for a in data if isinstance(a, dict) and a.get("accountNumber")]
