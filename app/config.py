import os
import re
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from dotenv import dotenv_values
from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_PAYNETICS_KEY = re.compile(r"^PAYNETICS_([A-Z0-9_]+)_ACCESS_TOKEN$")


class BankProviderName(StrEnum):
    MOCK = "mock"
    PAYNETICS = "paynetics"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    telegram_bot_token: SecretStr | None = None
    # Telegram user ids seeded into AllowedUser on startup as approvers, e.g. "123,456".
    bootstrap_approver_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    # Storage
    database_url: str = "sqlite+aiosqlite:///./data/app.db"
    companies_file: Path = Path("config/companies.yaml")

    # Bank
    bank_provider: BankProviderName = BankProviderName.MOCK
    paynetics_base_url: str = "https://api.paynetics.online/v1"
    # Credentials are per company (each has its own Paynetics account), read from
    # PAYNETICS_<CODE>_ACCESS_TOKEN / PAYNETICS_<CODE>_SECRET_KEY; see paynetics_credentials().
    poll_interval_seconds: int = 120
    poll_lookback_hours: int = 48
    poll_enabled: bool = True

    # Web admin panel (/admin). Disabled while ADMIN_PASSWORD is empty.
    admin_password: SecretStr | None = None
    # Signs the admin session cookie. If empty, a random one is generated per process.
    session_secret: SecretStr | None = None
    uploads_dir: Path = Path("data/uploads")
    # Set true when served over HTTPS (production) so the session cookie is Secure.
    https_only_cookies: bool = False

    # Public URL of this app, used in invoice links. Render sets RENDER_EXTERNAL_URL itself.
    public_base_url: str | None = Field(
        default=None, validation_alias=AliasChoices("PUBLIC_BASE_URL", "RENDER_EXTERNAL_URL")
    )

    # Claude API (free-text invoice parsing only; it never triggers actions)
    anthropic_api_key: SecretStr | None = None
    claude_model: str = "claude-sonnet-5-5"

    # Outgoing email (SMTP). Without SMTP_HOST, email deliveries are recorded as skipped.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_starttls: bool = True
    smtp_from: str | None = None  # fallback sender when a company has no email_from

    # Safety
    dry_run: bool = True
    dry_run_email: str | None = None
    max_invoice_amount: Decimal = Decimal("50000.00")
    max_payment_instruction_amount: Decimal = Decimal("50000.00")

    # Reminders (M6): overdue invoices and approved payments the bank hasn't shown yet.
    reminders_enabled: bool = True
    reminder_check_seconds: int = 3600
    reminder_repeat_hours: int = 24
    instruction_timeout_hours: int = 48

    default_currency: str = "EUR"

    @field_validator("bootstrap_approver_ids", mode="before")
    @classmethod
    def _split_ids(cls, v: object) -> object:
        if isinstance(v, str):
            return [int(x) for x in v.replace(" ", "").split(",") if x]
        if isinstance(v, int):
            return [v]
        return v

    def paynetics_credentials(self) -> dict[str, tuple[str, str]]:
        """{COMPANY_CODE: (access_token, secret_key)} from the environment and .env.

        Codes are upper-case; a "-" in a company code is written as "_" in the variable name.
        """
        env: dict[str, str] = {}
        env_file = self.model_config.get("env_file")
        if env_file and Path(str(env_file)).is_file():
            env.update({k: v for k, v in dotenv_values(str(env_file)).items() if v})
        env.update(os.environ)
        creds: dict[str, tuple[str, str]] = {}
        for key, token in env.items():
            m = _PAYNETICS_KEY.match(key)
            if not m or not token:
                continue
            secret = env.get(f"PAYNETICS_{m[1]}_SECRET_KEY")
            if secret:
                creds[m[1]] = (token.strip(), secret.strip())
        return creds

    @property
    def dev_commands_enabled(self) -> bool:
        return self.bank_provider == BankProviderName.MOCK


@lru_cache
def get_settings() -> Settings:
    return Settings()
