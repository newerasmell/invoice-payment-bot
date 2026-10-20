from app.bank.base import BankProvider
from app.bank.mock import MockBankProvider
from app.bank.paynetics import PayneticsClient, PayneticsProvider
from app.config import BankProviderName, Settings
from app.db.session import Database


def make_bank_provider(settings: Settings, db: Database) -> BankProvider:
    if settings.bank_provider == BankProviderName.MOCK:
        return MockBankProvider(db)
    clients = {
        code: PayneticsClient(code, settings.paynetics_base_url, token, secret)
        for code, (token, secret) in settings.paynetics_credentials().items()
    }
    return PayneticsProvider(clients)
