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
from app.core.security import hash_password
from app.db import close_client, ensure_indexes, get_db
from app.models.common import now
from app.routers import admin_apps, admin_auth, admin_categories, admin_stats, admin_versions, public, users
from app.services.cleanup import cleanup_loop
from app.services.notifications import PushService
from app.services.scanning import ScanQueue
from app.services.storage import create_storage

log = logging.getLogger("kaskad")


async def bootstrap_admin(db) -> None:
    """Crée le premier compte admin (ADMIN_EMAIL / ADMIN_PASSWORD) si aucun n'existe."""
    s = get_settings()
    if await db.admins.estimated_document_count() or not (s.admin_email and s.admin_password):
        return
    await db.admins.insert_one(
        {
            "email": s.admin_email.lower(),
            "password_hash": hash_password(s.admin_password),
            "name": "Administrator",
            "role": "admin",
            "active": True,
            "created_at": now(),
        }
    )
    log.info("Initial admin account created: %s", s.admin_email)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.is_production and (settings.jwt_secret == "change-me-in-production" or len(settings.jwt_secret) < 32):
        raise RuntimeError("JWT_SECRET must be set to a random value of at least 32 characters in production")
    db = get_db()
    await ensure_indexes(db)
    await bootstrap_admin(db)
    storage = create_storage(settings)
    await storage.init()

    app.state.db = db
    app.state.storage = storage
    app.state.push = PushService(settings)
    app.state.login_limiter = LoginRateLimiter(settings.login_max_attempts, settings.login_window_minutes * 60)
    app.state.scan_queue = ScanQueue(db, storage, settings)
    await app.state.scan_queue.start()
    cleanup_task = asyncio.create_task(cleanup_loop(db, storage, settings)) if settings.environment != "test" else None
    try:
        yield
    finally:
        await app.state.scan_queue.stop()
        if cleanup_task:
            cleanup_task.cancel()
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
            content={"detail": message(exc.code, language(request)), "code": exc.code, **exc.extra},
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
        admin_categories.router,
        admin_apps.router,
        admin_versions.router,
        admin_stats.router,
    ):
        app.include_router(router, prefix=settings.api_prefix)
    return app


app = create_app()
