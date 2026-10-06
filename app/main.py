"""Точка входа FastAPI-приложения."""
from __future__ import annotations

import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import artifacts, health, records, sync, tree, view
from .config import ensure_dirs, settings
from .logger import (
    get_access_logger,
    get_logger,
    setup_access_logging,
    setup_logging,
)

# --- Логирование до всего остального ---
setup_logging()
setup_access_logging()
log = get_logger(__name__)
access_log = get_access_logger()


def create_app() -> FastAPI:
    ensure_dirs()

    app = FastAPI(
        title="Screen Recorder Sync Server",
        version="1.0.0",
        description=(
            "Сервис синхронизации записей. Один пользователь, "
            "авторизация по X-API-Key, хранение — файловая система."
        ),
        docs_url="/docs" if settings.openapi_enabled else None,
        redoc_url="/redoc" if settings.openapi_enabled else None,
        openapi_url="/openapi.json" if settings.openapi_enabled else None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # --- middleware: access-log + request-id ---
    @app.middleware("http")
    async def access_middleware(request: Request, call_next):
        req_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:8]
        t0 = time.monotonic()

        response = await call_next(request)

        elapsed_ms = (time.monotonic() - t0) * 1000
        client = request.client.host if request.client else "-"
        access_log.info(
            "%s %s %s → %d (%.1f ms) [%s]",
            client,
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            req_id,
        )
        response.headers["X-Request-ID"] = req_id
        return response

    # --- middleware: глобальный обработчик исключений ---
    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        log.exception(
            "Необработанное исключение на %s %s: %s",
            request.method, request.url.path, exc,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )

    # --- health без авторизации ---
    @app.get("/health", tags=["health"])
    async def root_health():
        return {
            "status": "ok",
            "service": "screc-sync",
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    app.include_router(health.router, prefix="/api/v1")
    app.include_router(tree.router, prefix="/api/v1")
    app.include_router(records.router, prefix="/api/v1")
    app.include_router(artifacts.router, prefix="/api/v1")
    app.include_router(sync.router, prefix="/api/v1")

    # --- публичный просмотр записи (без авторизации) ---
    # Роутер view НЕ включается под /api/v1 и НЕ требует X-API-Key.
    # Доступ защищён совпадением id из URL с id в _meta.json.
    app.include_router(view.router)

    @app.on_event("startup")
    async def on_startup():
        log.info("=" * 60)
        log.info("Screen Recorder Sync Server запущен")
        log.info("data_root   = %s", settings.data_root)
        log.info("log_root    = %s", settings.log_root)
        log.info("api_key set = %s", bool(settings.api_key))
        log.info("fts enabled = %s", settings.fts_enabled)
        log.info("openapi     = %s", settings.openapi_enabled)

        # --- Инициализация индекса id → path ---
        try:
            from . import records_index
            records_index.ensure_initialized()
            log.info("Индекс записей готов")
        except Exception as exc:
            log.exception(
                "Не удалось инициализировать индекс записей: %s",
                exc,
            )

        log.info("=" * 60)

    @app.on_event("shutdown")
    async def on_shutdown():
        log.info("Screen Recorder Sync Server остановлен")

    return app


app = create_app()