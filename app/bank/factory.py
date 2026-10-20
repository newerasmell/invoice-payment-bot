from app.bank.base import BankProvider
from app.bank.mock import MockBankProvider
from app.config import BankProviderName, Settings
from app.db.session import Database


def make_bank_provider(settings: Settings, db: Database) -> BankProvider:
    if settings.bank_provider == BankProviderName.MOCK:
        return MockBankProvider(db)
    raise NotImplementedError("Paynetics provider arrives in M3; use BANK_PROVIDER=mock for now")
