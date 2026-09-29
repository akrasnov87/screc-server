"""Работа с путями внутри дерева records/.

Все имена сегментов (project, year, month, folder_name)
проходят через _safe_segment(). После сборки путь проверяется
через _assert_inside() — защита от path traversal.
"""
from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

from fastapi import HTTPException

from .config import settings

# Символы, недопустимые в именах файлов и папок на большинстве ФС.
_FORBIDDEN_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_segment(raw: str, *, max_bytes: int = 200) -> str:
    """
    Приводит сегмент пути к безопасному виду:
      • убирает управляющие символы и запрещённые символы;
      • схлопывает пробелы;
      • убирает ведущие/замыкающие точки и пробелы;
      • обрезает по границе UTF-8 (max_bytes).

    Пустая строка → "_".
    """
    if not raw:
        return "_"

    # NFC — чтобы «й» и «и + ̆» не считались разными.
    s = unicodedata.normalize("NFC", raw)

    # Запрещённые символы → "_".
    s = _FORBIDDEN_CHARS.sub("_", s)

    # Схлопываем пробелы.
    s = re.sub(r"\s+", " ", s).strip()

    # Ведущие/замыкающие точки — на Linux допустимы, но . и ..
    # опасны. Также замыкающая точка/пробел ломает Windows-клиентов.
    s = s.strip(". ")

    if not s:
        return "_"

    # Обрезаем по границе UTF-8, не разрывая символы.
    encoded = s.encode("utf-8")
    if len(encoded) > max_bytes:
        s = encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()
        if not s:
            s = "_"

    return s


def safe_relative_path(
    project: str,
    year: str,
    month: str,
    folder_name: str,
) -> Path:
    """Собирает относительный путь внутри records/."""
    return Path(
        _safe_segment(project, max_bytes=100),
        _safe_segment(year, max_bytes=10),
        _safe_segment(month, max_bytes=10),
        _safe_segment(folder_name, max_bytes=200),
    )


def record_dir(
    project: str,
    year: str,
    month: str,
    folder_name: str,
) -> Path:
    """Абсолютный путь к папке записи."""
    rel = safe_relative_path(project, year, month, folder_name)
    return settings.records_root / rel


def assert_inside(base: Path, target: Path) -> None:
    """
    Проверяет, что target находится внутри base.

    Бросает HTTPException(400), если это не так —
    защита от path traversal.
    """
    try:
        base_r = base.resolve()
        target_r = target.resolve()
    except OSError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid path: {exc}",
        )

    base_str = str(base_r)
    target_str = str(target_r)
    if target_str != base_str and not target_str.startswith(
        base_str + os.sep
    ):
        raise HTTPException(
            status_code=400,
            detail="Path traversal detected",
        )