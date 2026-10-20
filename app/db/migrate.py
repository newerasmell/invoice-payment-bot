import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[2]


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


async def upgrade_to_head(url: str) -> None:
    # Alembic's async env calls asyncio.run(), so it needs its own thread/event loop.
    await asyncio.to_thread(command.upgrade, alembic_config(url), "head")
