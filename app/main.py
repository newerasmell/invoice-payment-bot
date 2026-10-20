import asyncio
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import public as public_routes
from app import webhooks
from app.admin import routes as admin
from app.admin.auth import CsrfFailed, LoginRequired
from app.bank.factory import make_bank_provider
from app.bot.setup import BOT_COMMANDS, make_bot, make_dispatcher
from app.companies import load_companies_file, seed_approvers, seed_companies
from app.config import Settings, get_settings
from app.db.migrate import upgrade_to_head
from app.db.session import Database
from app.invoices.pdf import pdf_engine_status
from app.payments.poller import poll_forever

log = logging.getLogger("app")


async def bootstrap(settings: Settings) -> Database:
    db = Database(settings.database_url)
    await upgrade_to_head(settings.database_url)
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
            try:
                await bot.set_my_commands(BOT_COMMANDS)
            except Exception:  # the menu is cosmetic; never block startup on it
                log.warning("could not set the Telegram command menu", exc_info=True)
            polling = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
            app.state.bot = bot
            log.info(
                "telegram bot polling started (dry_run=%s, bank=%s)",
                settings.dry_run,
                settings.bank_provider.value,
            )
        else:
            log.warning("TELEGRAM_BOT_TOKEN not set; running API only")
        poller: asyncio.Task[None] | None = None
        if settings.poll_enabled:
            poller = asyncio.create_task(poll_forever(settings, db, bank, bot))
        try:
            yield
        finally:
            if poller:
                poller.cancel()
                with suppress(asyncio.CancelledError):
                    await poller
            if hasattr(bank, "aclose"):
                await bank.aclose()
            if polling:
                polling.cancel()
                with suppress(asyncio.CancelledError):
                    await polling
            if bot:
                await bot.session.close()
            await db.dispose()

    app = FastAPI(title="Invoice & Payment Bot", lifespan=lifespan)
    app.state.settings = settings

    if settings.session_secret:
        session_secret = settings.session_secret.get_secret_value()
    else:
        session_secret = secrets.token_urlsafe(32)
        log.warning("SESSION_SECRET not set; admin sessions will not survive a restart")
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        session_cookie="admin_session",
        max_age=12 * 3600,
        same_site="lax",
        https_only=settings.https_only_cookies,
    )
    app.mount("/admin/static", StaticFiles(directory=admin.STATIC_DIR), name="admin-static")
    app.include_router(admin.public)
    app.include_router(admin.router)
    app.include_router(public_routes.router)
    app.include_router(webhooks.router)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired) -> RedirectResponse:
        return RedirectResponse("/admin/login", status_code=303)

    @app.exception_handler(CsrfFailed)
    async def _csrf_failed(request: Request, exc: CsrfFailed) -> PlainTextResponse:
        return PlainTextResponse("Form expired, go back and reload the page.", status_code=400)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/admin", status_code=307)

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {
            "ok": True,
            "dry_run": settings.dry_run,
            "bank": settings.bank_provider.value,
            "pdf": pdf_engine_status(),
            "llm": bool(settings.anthropic_api_key),
            "email": bool(settings.smtp_host),
            "bank_accounts": sorted(settings.paynetics_credentials())
            if settings.bank_provider.value == "paynetics"
            else [],
        }

    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()
