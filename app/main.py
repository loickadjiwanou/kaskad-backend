import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.i18n import ApiError, language, message
from app.core.ratelimit import LoginRateLimiter
from app.db import close_client, ensure_indexes, get_db
from app.routers import admin_apps, admin_auth, admin_categories, admin_mfa, admin_stats, admin_versions, public, reviews, users, web
from app.services import accounts
from app.services.cleanup import cleanup_loop
from app.services.mailer import Mailer
from app.services.notifications import PushService
from app.services.releases import scheduler_loop
from app.services.scanning import ScanQueue
from app.services.storage import create_storage

log = logging.getLogger("kaskad")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.is_production and (settings.jwt_secret == "change-me-in-production" or len(settings.jwt_secret) < 32):
        raise RuntimeError("JWT_SECRET must be set to a random value of at least 32 characters in production")
    db = get_db()
    await ensure_indexes(db)
    await accounts.bootstrap(db)
    storage = create_storage(settings)
    await storage.init()

    app.state.db = db
    app.state.storage = storage
    app.state.push = PushService(settings)
    app.state.mailer = Mailer(settings)
    app.state.login_limiter = LoginRateLimiter(settings.login_max_attempts, settings.login_window_minutes * 60)
    # Inscriptions, renvois d'e-mail, invitations : limités par adresse IP
    app.state.signup_limiter = LoginRateLimiter(5, 60 * 60, max_per_ip=20)
    app.state.scan_queue = ScanQueue(db, storage, settings, app.state.mailer)
    await app.state.scan_queue.start()
    cleanup_task = asyncio.create_task(cleanup_loop(db, storage, settings)) if settings.environment != "test" else None
    # Publications programmées (les tests appellent directement releases.publish_due)
    scheduler_task = asyncio.create_task(scheduler_loop(db, app.state.push, app.state.mailer)) if settings.environment != "test" else None
    try:
        yield
    finally:
        await app.state.scan_queue.stop()
        if cleanup_task:
            cleanup_task.cancel()
        if scheduler_task:
            scheduler_task.cancel()
        await close_client()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Kaskad API",
        version="1.0.0",
        description="API du store Kaskad : catalogue public, comptes utilisateurs et administration.",
        lifespan=lifespan,
        docs_url=f"{settings.api_prefix}/docs",
        openapi_url=f"{settings.api_prefix}/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        # `detail` : message lisible dans la langue du client ; `code` : identifiant stable
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": message(exc.code, language(request), **exc.extra), "code": exc.code, **exc.extra},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "detail": "Données invalides." if language(request) == "fr" else "Invalid data.",
                "code": "validation_error",
                "errors": [{"loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()],
            },
        )

    for router in (
        public.router,
        users.router,
        admin_auth.router,
        admin_mfa.router,
        admin_categories.router,
        admin_apps.router,
        admin_versions.router,
        admin_stats.router,
        reviews.router,
    ):
        app.include_router(router, prefix=settings.api_prefix)
    # Pages web publiques des apps (liens partagés), hors préfixe de l'API
    app.include_router(web.router)
    return app


app = create_app()
