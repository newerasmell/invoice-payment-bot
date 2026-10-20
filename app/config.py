from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
    paynetics_access_token: SecretStr | None = None
    paynetics_secret_key: SecretStr | None = None
    poll_interval_seconds: int = 120

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

    default_currency: str = "EUR"

    @field_validator("bootstrap_approver_ids", mode="before")
    @classmethod
    def _split_ids(cls, v: object) -> object:
        if isinstance(v, str):
            return [int(x) for x in v.replace(" ", "").split(",") if x]
        if isinstance(v, int):
            return [v]
        return v

    @property
    def dev_commands_enabled(self) -> bool:
        return self.bank_provider == BankProviderName.MOCK


@lru_cache
def get_settings() -> Settings:
    return Settings()
