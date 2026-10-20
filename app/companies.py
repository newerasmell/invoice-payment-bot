from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.audit import audit
from app.db.models import AllowedUser, Company, CompanyBankAccount, InvoiceTemplate, UserRole

_COMPANY_FIELDS = {
    "code",
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
    "logo_path",
    "email_from",
    "mail_config_ref",
    "invoice_prefix",
    "invoice_next_number",
    "default_currency",
    "default_vat_rate",
    "no_vat_note",
    "default_payment_terms_days",
}


def load_companies_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


async def seed_companies(session: AsyncSession, data: dict[str, Any]) -> list[str]:
    """Insert companies that don't exist yet. Returns the codes that were created."""
    created: list[str] = []
    for spec in data.get("companies", []):
        code = str(spec["code"]).upper()
        if await session.scalar(select(Company.id).where(Company.code == code)):
            continue
        company = Company(**{k: v for k, v in spec.items() if k in _COMPANY_FIELDS})
        company.code = code
        session.add(company)
        company.bank_accounts = [CompanyBankAccount(**acc) for acc in spec.get("bank_accounts", [])]
        templates = [InvoiceTemplate(**tpl) for tpl in spec.get("templates", [])]
        company.templates = templates
        await session.flush()
        if templates:
            company.default_template_id = templates[0].id
        audit(session, "system", "company.seeded", f"company:{company.id}", {"code": code})
        created.append(code)
    await session.commit()
    return created


async def seed_approvers(session: AsyncSession, telegram_ids: list[int]) -> None:
    for tg_id in telegram_ids:
        if await session.scalar(select(AllowedUser.id).where(AllowedUser.telegram_id == tg_id)):
            continue
        session.add(AllowedUser(telegram_id=tg_id, role=UserRole.APPROVER))
        audit(session, "system", "user.bootstrapped", f"tg:{tg_id}", {"role": "approver"})
    await session.commit()


async def list_companies(session: AsyncSession) -> list[Company]:
    return list(
        (
            await session.scalars(
                select(Company).options(selectinload(Company.bank_accounts)).order_by(Company.code)
            )
        ).all()
    )


async def find_company(session: AsyncSession, key: str) -> Company | None:
    """Look a company up by code (X) or exact name, case-insensitively."""
    key = key.strip()
    return await session.scalar(
        select(Company)
        .options(selectinload(Company.bank_accounts))
        .where(
            (func.upper(Company.code) == key.upper()) | (func.lower(Company.name) == key.lower())
        )
    )


def default_bank_account(
    company: Company, currency: str | None = None
) -> CompanyBankAccount | None:
    accounts = company.bank_accounts
    if currency:
        same = [a for a in accounts if a.currency == currency]
        accounts = same or accounts
    return next((a for a in accounts if a.is_default), accounts[0] if accounts else None)
