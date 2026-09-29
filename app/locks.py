"""Блокировка операций записи через fcntl.flock.

Один пользователь — один .lock. Все операции записи
(создание/обновление/удаление записи, инкремент revision,
запись changelog) выполняются под LOCK_EX.

Чтение (meta.json, артефакты) — без лока: atomic_write
гарантирует, что читатель видит либо старое, либо новое
состояние файла, но не промежуточное.
"""
from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path

from .config import settings
from .logger import get_logger

log = get_logger(__name__)

_LOCK_FILE = "._lock"


@contextmanager
def write_lock():
    """
    Эксклюзивная блокировка на запись в дерево records/.

    Пример:
        with write_lock():
            rev = next_revision()
            atomic_write_json(...)
    """
    lock_path = settings.data_root / _LOCK_FILE
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    fd = open(lock_path, "w")
    try:
        log.debug("Запрос write-lock: %s", lock_path)
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        log.debug("write-lock получен")
        yield
    finally:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        finally:
            fd.close()
            log.debug("write-lock освобождён")