"""Монотонный счётчик revision и changelog (append-only)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .atomic import append_jsonl, atomic_write_text, iter_jsonl
from .config import settings
from .logger import get_logger

log = get_logger(__name__)

_REVISION_FILE = "revision.txt"
_CHANGELOG_FILE = "changelog.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _revision_path() -> Path:
    return settings.data_root / _REVISION_FILE


def _changelog_path() -> Path:
    return settings.data_root / _CHANGELOG_FILE


def get_revision() -> int:
    """Текущее значение счётчика (0, если файла нет)."""
    p = _revision_path()
    if not p.is_file():
        return 0
    try:
        return int(p.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return 0


def next_revision() -> int:
    """
    Увеличивает счётчик на 1 и возвращает новое значение.

    ВАЖНО: вызывать под write_lock().
    """
    new_rev = get_revision() + 1
    atomic_write_text(_revision_path(), str(new_rev))
    log.debug("revision → %d", new_rev)
    return new_rev


def append_change(
    *,
    entity: str,
    action: str,
    entity_id: str,
    path: str = "",
    payload: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> int:
    """
    Пишет строку в changelog.jsonl.

    ВАЖНО: вызывать под write_lock() — next_revision()
    должен идти в той же транзакции.
    """
    rev = next_revision()
    entry: Dict[str, Any] = {
        "rev": rev,
        "ts": _now_iso(),
        "entity": entity,
        "action": action,
        "id": entity_id,
        "path": path,
    }
    if payload is not None:
        entry["payload"] = payload
    if extra:
        entry.update(extra)

    append_jsonl(_changelog_path(), entry)
    log.info(
        "changelog: rev=%d, entity=%s, action=%s, id=%s, path=%s",
        rev, entity, action, entity_id, path or "—",
    )
    return rev


def iter_changes(since_revision: int, limit: int) -> Iterator[Dict[str, Any]]:
    """
    Итератор по строкам changelog с rev > since_revision.
    Останавливается после limit записей.
    """
    count = 0
    for entry in iter_jsonl(_changelog_path()):
        rev = entry.get("rev", 0)
        if rev <= since_revision:
            continue
        yield entry
        count += 1
        if count >= limit:
            break


def read_changes(
    since_revision: int,
    limit: int,
) -> List[Dict[str, Any]]:
    return list(iter_changes(since_revision, limit))