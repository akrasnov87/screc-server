"""Инвертированный индекс в JSON.

Файл data/fts.json:
  {
    "version": 1,
    "records": {
      "<record_id>": {
        "path": "...",
        "project": "vNext",
        "date": "2026-09-08",
        "name": "...",
        "tokens": ["формирование", "реестр", ...]
      }
    },
    "index": {
      "реестр": ["<record_id>", ...],
      ...
    }
  }
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Iterable, List, Set

from .atomic import atomic_write_json, read_json
from .config import settings
from .logger import get_logger

log = get_logger(__name__)

_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+", re.UNICODE)

_fts_path: Path | None = None


def _path() -> Path:
    global _fts_path
    if _fts_path is None:
        _fts_path = settings.data_root / "fts.json"
    return _fts_path


def _tokenize(text: str) -> List[str]:
    return [_normalize(t) for t in _TOKEN_RE.findall(text or "")]


def _normalize(word: str) -> str:
    w = word.lower().strip()
    if len(w) < 2:
        return w
    # схлопываем 3+ повторов подряд: "аааа" → "аа"
    return re.sub(r"(.)\1{2,}", r"\1", w)


def _load() -> Dict:
    return read_json(_path(), default={
        "version": 1, "records": {}, "index": {}
    }) or {"version": 1, "records": {}, "index": {}}


def _save(data: Dict) -> None:
    atomic_write_json(_path(), data)


def index_record(
    record_id: str,
    *,
    path: str,
    project: str,
    date: str,
    name: str,
    texts: Iterable[str],
) -> None:
    """Добавляет/обновляет запись в индексе."""
    if not settings.fts_enabled:
        return

    data = _load()

    # удаляем старые токены
    old = data["records"].pop(record_id, None)
    if old:
        for token in old.get("tokens", []):
            ids = data["index"].get(token, [])
            if record_id in ids:
                ids.remove(record_id)
            if not ids:
                data["index"].pop(token, None)

    # собираем новые токены
    tokens: Set[str] = set()
    for text in texts:
        for t in _tokenize(text):
            if t:
                tokens.add(t)

    if name:
        for t in _tokenize(name):
            tokens.add(t)

    data["records"][record_id] = {
        "path": path,
        "project": project,
        "date": date,
        "name": name,
        "tokens": sorted(tokens),
    }

    for token in tokens:
        data["index"].setdefault(token, []).append(record_id)

    _save(data)
    log.debug(
        "FTS: проиндексировано %s, токенов=%d, путь=%s",
        record_id, len(tokens), path,
    )


def remove_record(record_id: str) -> None:
    if not settings.fts_enabled:
        return
    data = _load()
    old = data["records"].pop(record_id, None)
    if not old:
        return
    for token in old.get("tokens", []):
        ids = data["index"].get(token, [])
        if record_id in ids:
            ids.remove(record_id)
        if not ids:
            data["index"].pop(token, None)
    _save(data)
    log.debug("FTS: удалено %s", record_id)


def move_record(record_id: str, new_path: str) -> None:
    if not settings.fts_enabled:
        return
    data = _load()
    rec = data["records"].get(record_id)
    if not rec:
        return
    rec["path"] = new_path
    _save(data)


def search(
    query: str,
    *,
    project: str = "",
    limit: int = 100,
) -> List[Dict]:
    """
    Возвращает список записей, отсортированных по релевантности.

    Релевантность = число различных токенов запроса, найденных
    в записи.
    """
    if not settings.fts_enabled or not query.strip():
        return []

    data = _load()
    tokens = set(_tokenize(query))
    if not tokens:
        return []

    scores: Dict[str, int] = {}
    for token in tokens:
        for rid in data["index"].get(token, []):
            scores[rid] = scores.get(rid, 0) + 1

    results: List[Dict] = []
    for rid, score in sorted(scores.items(), key=lambda x: -x[1]):
        rec = data["records"].get(rid)
        if not rec:
            continue
        if project and rec.get("project") != project:
            continue
        results.append({
            "id": rid,
            "path": rec.get("path", ""),
            "project": rec.get("project", ""),
            "date": rec.get("date", ""),
            "name": rec.get("name", ""),
            "score": score,
        })
        if len(results) >= limit:
            break

    log.info(
        "FTS-поиск: query=%r, tokens=%d, найдено=%d",
        query, len(tokens), len(results),
    )
    return results