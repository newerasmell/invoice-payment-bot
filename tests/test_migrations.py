import sqlite3
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from app.db.migrate import upgrade_to_head
from app.db.models import Base


async def test_migrations_match_models(tmp_path: Path) -> None:
    """Fails when a model changes without a matching migration (run `alembic revision
    --autogenerate`)."""
    db = tmp_path / "m.db"
    await upgrade_to_head(f"sqlite+aiosqlite:///{db}")
    engine = create_engine(f"sqlite:///{db}")
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    engine.dispose()
    assert diff == []


async def test_upgrade_is_idempotent_on_existing_data(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{db}"
    await upgrade_to_head(url)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO companies (code, name, invoice_prefix, invoice_next_number,"
            " default_currency, default_vat_rate, default_payment_terms_days, created_at,"
            " updated_at) VALUES ('X', 'X', '', 1, 'EUR', 20, 14, '2026-10-20', '2026-10-20')"
        )
    await upgrade_to_head(url)  # a restart must not touch existing rows
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM companies").fetchone() == (1,)
