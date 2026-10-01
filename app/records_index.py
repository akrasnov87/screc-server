"""Индекс id → путь к папке записи.

Зачем:
  • `find_by_id` в records_service обходил `rglob` всего дерева
    на каждый запрос — при тысячах записей это узкое место.
  • Решение — отдельный индексный файл `data/records_index.json`,
    который хранит {record_id: relative_path} и кэшируется
    в памяти с инвалидацией по mtime.

Структура `records_index.json`:
    {
      "version": 1,
      "updated_at": "2026-10-01T12:00:00+00:00",
      "records": {
        "<record_id>": "vNext/2026/09/2026-09-08 формирование",
        ...
      }
    }

Гарантии:
  • Файл обновляется ТОЛЬКО под `write_lock()` — так же, как
    `_meta.json` и `changelog.jsonl`.
  • При старте (или при первом обращении) индекс загружается
    в память.
  • Если mtime файла изменился — кэш перечитывается.
  • Если индекс отсутствует или рассинхронизирован — есть
    метод `rebuild_from_tree()` для полной пересборки.
  • Если id в индексе нет — `find_by_id` падает на старый
    `rglob` как fallback и восстанавливает индекс.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from .atomic import atomic_write_json, read_json
from .config import settings
from .logger import get_logger

log = get_logger(__name__)

_INDEX_FILENAME = "records_index.json"

# In-memory кэш.
_lock = threading.RLock()
_cache: Optional[Dict[str, str]] = None
_cache_mtime: float = 0.0


def _index_path() -> Path:
    return settings.data_root / _INDEX_FILENAME


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty() -> Dict:
    return {
        "version": 1,
        "updated_at": _now_iso(),
        "records": {},
    }


# ---------------------------------------------------------------------------
# Загрузка / сохранение
# ---------------------------------------------------------------------------
def _load_from_disk() -> Dict[str, str]:
    """Читает индекс с диска. Возвращает {id: relative_path}."""
    path = _index_path()
    if not path.is_file():
        return {}
    data = read_json(path, default=None)
    if not isinstance(data, dict):
        log.warning(
            "Индекс записей повреждён (%s) — пересоберём из дерева",
            path,
        )
        return {}
    records = data.get("records")
    if not isinstance(records, dict):
        return {}
    # Приводим к {str: str}
    result: Dict[str, str] = {}
    for rid, rel in records.items():
        if isinstance(rid, str) and isinstance(rel, str):
            result[rid] = rel
    return result


def _save_to_disk(records: Dict[str, str]) -> None:
    """Пишет индекс на диск. ВАЖНО: вызывать под write_lock()."""
    path = _index_path()
    payload = {
        "version": 1,
        "updated_at": _now_iso(),
        "records": records,
    }
    atomic_write_json(path, payload)


def _get_cache() -> Dict[str, str]:
    """
    Возвращает актуальный индекс из памяти.
    Перечитывает с диска, если файл изменился.
    """
    global _cache, _cache_mtime
    path = _index_path()

    with _lock:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0

        if _cache is not None and mtime == _cache_mtime:
            return _cache

        _cache = _load_from_disk()
        _cache_mtime = mtime
        return _cache


def _invalidate_cache() -> None:
    global _cache, _cache_mtime
    with _lock:
        _cache = None
        _cache_mtime = 0.0


# ---------------------------------------------------------------------------
# Публичное API
# ---------------------------------------------------------------------------
def get_path(record_id: str) -> Optional[str]:
    """
    Возвращает относительный путь к папке записи (относительно
    `records_root`) или None, если id не найден.
    """
    if not record_id:
        return None
    return _get_cache().get(record_id)


def add(record_id: str, relative_path: str) -> None:
    """
    Добавляет/обновляет запись в индексе.

    ВАЖНО: вызывать под write_lock().
    """
    if not record_id or not relative_path:
        return
    records = dict(_get_cache())
    records[record_id] = relative_path
    _save_to_disk(records)
    _invalidate_cache()
    log.debug(
        "Индекс: добавлено/обновлено %s → %s",
        record_id, relative_path,
    )


def remove(record_id: str) -> None:
    """
    Удаляет запись из индекса.

    ВАЖНО: вызывать под write_lock().
    """
    if not record_id:
        return
    records = dict(_get_cache())
    if record_id not in records:
        return
    records.pop(record_id, None)
    _save_to_disk(records)
    _invalidate_cache()
    log.debug("Индекс: удалено %s", record_id)


def rebuild_from_tree() -> int:
    """
    Полностью пересобирает индекс, обходя дерево records/.

    ВАЖНО: вызывать под write_lock().

    Возвращает количество записей в индексе.
    """
    root = settings.records_root
    records: Dict[str, str] = {}

    if root.is_dir():
        for meta_path in root.rglob("_meta.json"):
            data = read_json(meta_path, default={}) or {}
            rid = data.get("id")
            if not isinstance(rid, str) or not rid:
                continue
            try:
                rel = str(
                    meta_path.parent.relative_to(root)
                ).replace("\\", "/")
            except ValueError:
                continue
            records[rid] = rel

    _save_to_disk(records)
    _invalidate_cache()
    log.info(
        "Индекс записей пересобран: %d записей", len(records),
    )
    return len(records)


def ensure_initialized() -> None:
    """
    Гарантирует, что индекс существует и не пуст, если в дереве
    есть записи. Вызывается при старте приложения.
    """
    path = _index_path()
    if path.is_file():
        # Уже есть — не трогаем.
        _invalidate_cache()
        _get_cache()
        return

    log.info("Индекс записей отсутствует — пересобираем из дерева")
    rebuild_from_tree()