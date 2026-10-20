import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from app.bank.factory import make_bank_provider
from app.bot.setup import make_bot, make_dispatcher
from app.companies import load_companies_file, seed_approvers, seed_companies
from app.config import Settings, get_settings
from app.db.session import Database, create_schema

log = logging.getLogger("app")


async def bootstrap(settings: Settings) -> Database:
    db = Database(settings.database_url)
    await create_schema(db.engine)
    async with db.session() as s:
        created = await seed_companies(s, load_companies_file(settings.companies_file))
        if created:
            log.info("seeded companies: %s", ", ".join(created))
        await seed_approvers(s, settings.bootstrap_approver_ids)
    return db


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = await bootstrap(settings)
        bank = make_bank_provider(settings, db)
        app.state.db, app.state.bank = db, bank
        polling: asyncio.Task[None] | None = None
        bot = None
        if settings.telegram_bot_token:
            bot = make_bot(settings.telegram_bot_token.get_secret_value())
            dp = make_dispatcher(settings, db, bank)
            polling = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
            log.info(
                "telegram bot polling started (dry_run=%s, bank=%s)",
                settings.dry_run,
                settings.bank_provider.value,
            )
        else:
            log.warning("TELEGRAM_BOT_TOKEN not set; running API only")
        try:
            yield
        finally:
            if polling:
                polling.cancel()
                with suppress(asyncio.CancelledError):
                    await polling
            if bot:
                await bot.session.close()
            await db.dispose()

    app = FastAPI(title="Invoice & Payment Bot", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {"ok": True, "dry_run": settings.dry_run, "bank": settings.bank_provider.value}

    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()
