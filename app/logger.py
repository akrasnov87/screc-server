"""Логирование с ротацией.

Пишем:
  • screc.log        — всё (DEBUG и выше);
  • screc.error.log  — только WARNING и выше;
  • access/access.log — HTTP-запросы.

Если файл недоступен (нет прав, диск полон) — автоматически
переключаемся на stdout, чтобы сервис не падал из-за логов.

Формат:
  2026-09-29 12:34:56.789 [INFO    ] app.api.records: сообщение
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import settings

_LOG_FORMAT = (
    "%(asctime)s.%(msecs)03d [%(levelname)-8s] "
    "%(name)s: %(message)s"
)
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_ACCESS_FORMAT = '%(asctime)s.%(msecs)03d [ACCESS] %(message)s'

_root_logger: logging.Logger | None = None
_access_logger: logging.Logger | None = None


def _try_mkdir(path: Path) -> bool:
    """Пытается создать каталог. Не падает при ошибке."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        return True
    except (OSError, PermissionError) as exc:
        # Пишем в stderr — логгер ещё не работает.
        print(
            f"[logger] Не удалось создать {path}: {exc}. "
            f"Логи пойдут только в stdout.",
            file=sys.stderr,
        )
        return False


def _try_file_handler(
    path: Path,
    *,
    level: int,
    formatter: logging.Formatter,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Handler | None:
    """Создаёт RotatingFileHandler. При ошибке возвращает None."""
    try:
        handler = RotatingFileHandler(
            path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        handler.setLevel(level)
        handler.setFormatter(formatter)
        return handler
    except (OSError, PermissionError) as exc:
        print(
            f"[logger] Не удалось открыть {path}: {exc}. "
            f"Использую stdout.",
            file=sys.stderr,
        )
        return None


def setup_logging() -> logging.Logger:
    """Инициализирует корневой логгер. Идемпотентен."""
    global _root_logger
    if _root_logger is not None:
        return _root_logger

    logger = logging.getLogger("screc")
    logger.setLevel(
        getattr(logging, settings.log_level.upper(), logging.INFO)
    )
    logger.propagate = False

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    # --- файлы (если возможно) ---
    logs_ok = _try_mkdir(settings.log_root)
    access_dir_ok = (
        logs_ok and _try_mkdir(settings.log_root / "access")
    )

    if logs_ok:
        main_handler = _try_file_handler(
            settings.log_root / "screc.log",
            level=logging.DEBUG,
            formatter=formatter,
        )
        if main_handler is not None:
            logger.addHandler(main_handler)

        error_handler = _try_file_handler(
            settings.log_root / "screc.error.log",
            level=logging.WARNING,
            formatter=formatter,
        )
        if error_handler is not None:
            logger.addHandler(error_handler)

    # --- консоль (всегда) ---
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(
        getattr(logging, settings.log_level.upper(), logging.INFO)
    )
    console.setFormatter(formatter)
    logger.addHandler(console)

    _root_logger = logger
    logger.info(
        "Логгер инициализирован: level=%s, logs=%s (files=%s), "
        "data=%s",
        settings.log_level,
        settings.log_root,
        logs_ok,
        settings.data_root,
    )
    return logger


def get_logger(name: str) -> logging.Logger:
    """
    Возвращает дочерний логгер.

    Использование:
        from app.logger import get_logger
        log = get_logger(__name__)
    """
    if not name.startswith("screc"):
        name = f"screc.{name}"
    return logging.getLogger(name)


# ---------------------------------------------------------------------------
# Access-логгер
# ---------------------------------------------------------------------------
def setup_access_logging() -> logging.Logger:
    global _access_logger
    if _access_logger is not None:
        return _access_logger

    logger = logging.getLogger("screc.access")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    formatter = logging.Formatter(_ACCESS_FORMAT, datefmt=_DATE_FORMAT)

    access_dir = settings.log_root / "access"
    if _try_mkdir(access_dir):
        handler = _try_file_handler(
            access_dir / "access.log",
            level=logging.INFO,
            formatter=formatter,
            max_bytes=10 * 1024 * 1024,
            backup_count=10,
        )
        if handler is not None:
            logger.addHandler(handler)

    # Если файл не получилось открыть — access-лог пойдёт в stdout
    # через консольный handler.
    if not logger.handlers:
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.INFO)
        console.setFormatter(formatter)
        logger.addHandler(console)

    _access_logger = logger
    return logger


def get_access_logger() -> logging.Logger:
    if _access_logger is None:
        return setup_access_logging()
    return _access_logger