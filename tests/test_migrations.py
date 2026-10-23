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


async def test_m3_upgrade_with_existing_rows(tmp_path: Path) -> None:
    import asyncio

    from alembic import command

    from app.db.migrate import alembic_config

    db = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{db}"
    await asyncio.to_thread(command.upgrade, alembic_config(url), "d7136a890cb2")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO bank_transfers (id, provider, external_id, direction, status, amount,"
            " currency, raw_json, created_at, updated_at) VALUES (1, 'mock', 'T1', 'incoming',"
            " 'processed', 1000, 'EUR', '{}', '2026-10-20', '2026-10-20')"
        )
        conn.execute(
            "INSERT INTO payment_matches (id, transfer_id, confidence, method, created_at,"
            " updated_at) VALUES (1, 1, 'LOW', 'no match', '2026-10-20', '2026-10-20')"
        )
    await upgrade_to_head(url)
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT company_id, account, amount FROM bank_transfers"
        ).fetchone() == (
            None,
            None,
            1000,
        )
        assert conn.execute("SELECT count(*) FROM payment_matches").fetchone() == (1,)


async def test_m5_upgrade_with_existing_instruction(tmp_path: Path) -> None:
    import asyncio

    from alembic import command

    from app.db.migrate import alembic_config

    db = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{db}"
    await asyncio.to_thread(command.upgrade, alembic_config(url), "b7f517ececf5")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO companies (id, code, name, invoice_prefix, invoice_next_number,"
            " default_currency, default_vat_rate, default_payment_terms_days, created_at,"
            " updated_at) VALUES (1, 'X', 'X', '', 1, 'EUR', 20, 14, '2026-10-20', '2026-10-20')"
        )
        conn.execute(
            "INSERT INTO payment_instructions (id, from_company_id, to_iban, to_name, amount,"
            " currency, reference, status, created_at, updated_at) VALUES (1, 1,"
            " 'BG80BNBG96611020345678', 'Supplier', 120000, 'EUR', 'INV 1', 'PROPOSED',"
            " '2026-10-20', '2026-10-20')"
        )
    await upgrade_to_head(url)
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT status, amount, supplier_invoice_number, executed_at FROM payment_instructions"
        ).fetchone()
        assert row == ("PROPOSED", 120000, None, None)


async def test_m5_payout_upgrade_with_existing_rule_run(tmp_path: Path) -> None:
    import asyncio

    from alembic import command

    from app.db.migrate import alembic_config

    db = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{db}"
    await asyncio.to_thread(command.upgrade, alembic_config(url), "ec7859e188e0")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO rule_runs (id, rule_id, transfer_id, status, invoice_id, detail,"
            " created_at) VALUES (1, 1, 1, 'drafted', 5, NULL, '2026-10-20')"
        )
    await upgrade_to_head(url)
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT status, invoice_id, instruction_id FROM rule_runs").fetchone()
        assert row == ("drafted", 5, None)


async def test_m6_upgrade_with_existing_invoice_and_instruction(tmp_path: Path) -> None:
    import asyncio

    from alembic import command

    from app.db.migrate import alembic_config

    db = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{db}"
    await asyncio.to_thread(command.upgrade, alembic_config(url), "ab0022d9dd41")
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
            " updated_at) VALUES (1, 1, 1, '', 10000, 20, 2000, 12000, 'EUR', 'SENT', 'manual',"
            " '2026-10-20', '2026-10-20')"
        )
        conn.execute(
            "INSERT INTO payment_instructions (id, from_company_id, to_iban, to_name, amount,"
            " currency, reference, status, created_at, updated_at) VALUES (1, 1,"
            " 'BG80BNBG96611020345678', 'Supplier', 120000, 'EUR', 'INV 1', 'AWAITING_BANK',"
            " '2026-10-20', '2026-10-20')"
        )
    await upgrade_to_head(url)
    with sqlite3.connect(db) as conn:
        for table in ("invoices", "payment_instructions"):
            row = conn.execute(
                f"SELECT status, first_approved_by, first_approved_at, reminded_at FROM {table}"
            ).fetchone()
            assert row[1:] == (None, None, None) and row[0] in ("SENT", "AWAITING_BANK")


async def test_credit_note_columns_on_existing_invoice(tmp_path: Path) -> None:
    import asyncio

    from alembic import command

    from app.db.migrate import alembic_config

    db = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{db}"
    await asyncio.to_thread(command.upgrade, alembic_config(url), "0335ef0464d9")
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
            "INSERT INTO invoices (id, number, issuer_id, counterparty_id, description,"
            " amount_net, vat_rate, amount_vat, amount_gross, currency, status, source,"
            " created_at, updated_at) VALUES (1, '0000000006', 1, 1, '', 10000, 20, 2000, 12000,"
            " 'EUR', 'APPROVED', 'manual', '2026-10-20', '2026-10-20')"
        )
    await upgrade_to_head(url)
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT number, kind, credited_invoice_id FROM invoices").fetchone()
        assert row == ("0000000006", "invoice", None)


async def test_request_columns_on_existing_client(tmp_path: Path) -> None:
    import asyncio

    from alembic import command

    from app.db.migrate import alembic_config

    db = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{db}"
    await asyncio.to_thread(command.upgrade, alembic_config(url), "2a5cc9737065")
    with sqlite3.connect(db) as conn:
        for i in (1, 2):  # two rows: the new unique index must accept many NULL tokens
            conn.execute(
                "INSERT INTO counterparties (id, name, archived, created_at, updated_at)"
                f" VALUES ({i}, 'Client {i}', 0, '2026-10-20', '2026-10-20')"
            )
    await upgrade_to_head(url)
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT name, request_token, request_companies FROM counterparties"
        ).fetchall()
        assert rows == [("Client 1", None, None), ("Client 2", None, None)]
        assert conn.execute("SELECT count(*) FROM invoice_requests").fetchone() == (0,)
