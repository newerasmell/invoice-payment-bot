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


async def test_m2_upgrade_with_existing_rows(tmp_path: Path) -> None:
    """The client/invoice columns added after the first deploy must apply to tables that
    already hold rows (production had data by then)."""
    from alembic import command

    from app.db.migrate import alembic_config

    db = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{db}"
    cfg = alembic_config(url)
    import asyncio

    await asyncio.to_thread(command.upgrade, cfg, "317caca0caaf")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO companies (id, code, name, invoice_prefix, invoice_next_number,"
            " default_currency, default_vat_rate, default_payment_terms_days, created_at,"
            " updated_at) VALUES (1, 'X', 'X', '', 1, 'EUR', 20, 14, '2026-10-20', '2026-10-20')"
        )
        conn.execute(
            "INSERT INTO counterparties (id, name, created_at, updated_at)"
            " VALUES (1, 'Client Ltd', '2026-10-20', '2026-10-20')"
        )
        conn.execute(
            "INSERT INTO invoices (id, issuer_id, counterparty_id, description, amount_net,"
            " vat_rate, amount_vat, amount_gross, currency, status, source, created_at,"
            " updated_at) VALUES (1, 1, 1, '', 10000, 20, 2000, 12000, 'EUR', 'DRAFT', 'manual',"
            " '2026-10-20', '2026-10-20')"
        )
    await upgrade_to_head(url)
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT archived, delivery_channels FROM counterparties"
        ).fetchone() == (
            0,
            None,
        )
        assert conn.execute("SELECT public_token, amount_gross FROM invoices").fetchone() == (
            None,
            12000,
        )
