"""Простая авторизация по API-ключу.

Ключ один на весь сервис (один пользователь). Передаётся
в заголовке X-API-Key. Сравнение — constant-time.

Исключения (не требуют ключа):
  • GET /health
  • GET /docs, /openapi.json (если SCREC_OPENAPI_ENABLED=true)
"""
from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from .config import settings
from .logger import get_logger

log = get_logger(__name__)


async def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """
    FastAPI-зависимость. Проверяет наличие и корректность ключа.

    Использование:
        @router.get("/records", dependencies=[Depends(require_api_key)])
        async def list_records(): ...
    """
    if not x_api_key:
        log.warning("Отказ: отсутствует заголовок X-API-Key")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header",
        )

    expected = settings.api_key or ""
    if not expected:
        log.error(
            "SCREC_API_KEY не задан в конфиге — сервис не может "
            "проверять авторизацию. Отказ."
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server misconfigured: API key not set",
        )

    if not hmac.compare_digest(x_api_key, expected):
        # Не логируем сам ключ ни в каком виде.
        log.warning(
            "Отказ: неверный API-ключ (получено %d символов, "
            "ожидалось %d)",
            len(x_api_key), len(expected),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    log.debug("Авторизация: OK")