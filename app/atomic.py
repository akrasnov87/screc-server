"""Атомарная запись файлов: tmp + os.replace."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .logger import get_logger

log = get_logger(__name__)


def _fsync_dir(path: Path) -> None:
    """fsync каталога — чтобы переименование пережило краш."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        # На некоторых ФС (например, tmpfs) это не поддерживается —
        # не считаем это ошибкой.
        pass


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path, data: Any) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    atomic_write_text(path, text)


def append_jsonl(path: Path, obj: Any) -> None:
    """Append одной строки в JSONL. POSIX гарантирует атомарность
    для записей ≤ PIPE_BUF (обычно 4096 байт)."""
    line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Не удалось прочитать JSON %s: %s", path, exc)
        return default


def iter_jsonl(path: Path):
    """Итератор по строкам JSONL. Битые строки пропускает."""
    if not path.is_file():
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue