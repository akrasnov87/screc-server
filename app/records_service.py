"""Бизнес-логика записей: создание, обновление, чтение, удаление."""
from __future__ import annotations

import hashlib
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from . import fts
from .atomic import (
    atomic_write_bytes,
    atomic_write_json,
    read_json,
)
from .config import settings
from .locks import write_lock
from .logger import get_logger
from .models import ArtifactInfo, RecordFull, RecordPayload
from .paths import assert_inside, record_dir, safe_relative_path
from .revision import append_change, get_revision

log = get_logger(__name__)

_META = "_meta.json"
_LINKS = "_links.json"
_DELETED = "_deleted.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Чтение
# ---------------------------------------------------------------------------
def read_meta(rdir: Path) -> Optional[Dict[str, Any]]:
    return read_json(rdir / _META)


def read_links(rdir: Path) -> Dict[str, Any]:
    return read_json(rdir / _LINKS, default={}) or {}


def is_deleted(rdir: Path) -> bool:
    return (rdir / _DELETED).is_file()


def build_full(rdir: Path) -> Optional[RecordFull]:
    """Читает _meta.json и _links.json → RecordFull."""
    meta = read_meta(rdir)
    if not meta:
        return None

    links = read_links(rdir)
    rel = rdir.relative_to(settings.records_root)

    return RecordFull(
        id=meta.get("id", ""),
        revision=int(meta.get("revision", 0)),
        project=meta.get("project", ""),
        year=meta.get("year", ""),
        month=meta.get("month", ""),
        folder_name=meta.get("folder_name", ""),
        path=str(rel).replace("\\", "/"),
        name=meta.get("name", ""),
        description=meta.get("description", ""),
        comment=meta.get("comment", ""),
        source=meta.get("source", "record"),
        is_scrum=bool(meta.get("is_scrum", False)),
        date=meta.get("date", ""),
        time=meta.get("time", ""),
        created_at=meta.get("created_at", ""),
        updated_at=meta.get("updated_at", ""),
        tags=list(meta.get("tags", []) or []),
        summary_bb=meta.get("summary_bb", ""),
        prompt=meta.get("prompt", ""),
        prompt_name=meta.get("prompt_name", ""),
        prompt_edited=bool(meta.get("prompt_edited", False)),
        name_template=meta.get("name_template", ""),
        name_abbr=meta.get("name_abbr", ""),
        generate_summary=bool(meta.get("generate_summary", False)),
        generate_deepseek_prompt=bool(
            meta.get("generate_deepseek_prompt", True)
        ),
        include_name_in_prompt=bool(
            meta.get("include_name_in_prompt", True)
        ),
        include_project_in_prompt=bool(
            meta.get("include_project_in_prompt", True)
        ),
        include_comment_in_prompt=bool(
            meta.get("include_comment_in_prompt", True)
        ),
        include_tags_in_prompt=bool(
            meta.get("include_tags_in_prompt", True)
        ),
        artifacts=[
            ArtifactInfo(**a) for a in meta.get("artifacts", []) or []
        ],
        video=links.get("video"),
    )


def find_by_id(record_id: str) -> Optional[Path]:
    """
    Ищет папку записи по id.

    Полный обход дерева — приемлемо, потому что пользователь один
    и записей немного (тысячи, не миллионы). Кэш можно добавить
    позже, если станет узким местом.
    """
    if not settings.records_root.is_dir():
        return None
    for meta_path in settings.records_root.rglob(_META):
        meta = read_json(meta_path, default={}) or {}
        if meta.get("id") == record_id:
            return meta_path.parent
    return None


# ---------------------------------------------------------------------------
# Создание / обновление
# ---------------------------------------------------------------------------
async def create_or_update(
    *,
    payload: RecordPayload,
    files: List[Tuple[str, str, bytes]],   # (kind, filename, bytes)
) -> Dict[str, Any]:
    """
    Создаёт или обновляет запись. Идемпотентно по (path).
    Возвращает {"id": ..., "revision": ..., "action": ...,
                "path": ..., "artifacts": [...]}.
    """
    rdir = record_dir(
        payload.project, payload.year,
        payload.month, payload.folder_name,
    )
    assert_inside(settings.records_root, rdir)
    rel_path = str(rdir.relative_to(settings.records_root)).replace(
        "\\", "/"
    )

    with write_lock():
        # --- определяем, создаём или обновляем ---
        existing_meta = read_meta(rdir)
        if existing_meta:
            record_id = existing_meta.get("id") or str(uuid.uuid4())
            action = "update"
            created_at = existing_meta.get("created_at") or _now_iso()
            log.info(
                "Обновление записи: id=%s, path=%s",
                record_id, rel_path,
            )
        else:
            record_id = payload.id or str(uuid.uuid4())
            action = "create"
            created_at = _now_iso()
            log.info(
                "Создание записи: id=%s, path=%s",
                record_id, rel_path,
            )

        rdir.mkdir(parents=True, exist_ok=True)

        # --- артефакты ---
        artifacts_meta: List[Dict[str, Any]] = []
        for kind, filename, content in files:
            # санитизация имени файла
            safe_name = Path(filename).name
            if not safe_name:
                raise HTTPException(400, "Empty artifact filename")
            dest = rdir / safe_name
            assert_inside(rdir, dest)
            atomic_write_bytes(dest, content)
            artifacts_meta.append({
                "kind": kind,
                "filename": safe_name,
                "size": len(content),
                "sha256": _sha256(content),
            })
            log.info(
                "Артефакт сохранён: record=%s, kind=%s, name=%s, "
                "size=%d",
                record_id, kind, safe_name, len(content),
            )

        # --- _meta.json ---
        meta: Dict[str, Any] = {
            "id": record_id,
            "project": payload.project,
            "year": payload.year,
            "month": payload.month,
            "folder_name": payload.folder_name,

            "name": payload.name,
            "description": payload.description,
            "comment": payload.comment,
            "source": payload.source,
            "is_scrum": payload.is_scrum,

            "date": payload.date,
            "time": payload.time,
            "created_at": created_at,
            "updated_at": _now_iso(),

            "tags": payload.tags,

            "summary_bb": payload.summary_bb,
            "prompt": payload.prompt,
            "prompt_name": payload.prompt_name,
            "prompt_edited": payload.prompt_edited,
            "name_template": payload.name_template,
            "name_abbr": payload.name_abbr,

            "generate_summary": payload.generate_summary,
            "generate_deepseek_prompt": payload.generate_deepseek_prompt,
            "include_name_in_prompt": payload.include_name_in_prompt,
            "include_project_in_prompt": payload.include_project_in_prompt,
            "include_comment_in_prompt": payload.include_comment_in_prompt,
            "include_tags_in_prompt": payload.include_tags_in_prompt,

            "artifacts": artifacts_meta,
            "revision": 0,   # проставим ниже
            "deleted_at": None,
        }

        # --- _links.json ---
        links: Dict[str, Any] = read_links(rdir) or {}
        if payload.video_url:
            links["video"] = {
                "url": payload.video_url,
                "size": payload.video_size,
                "mime": payload.video_mime,
                "duration": payload.video_duration,
                "updated_at": _now_iso(),
            }
        else:
            links.setdefault("video", None)
        links["updated_at"] = _now_iso()

        # --- changelog (получаем revision) ---
        rev = append_change(
            entity="record",
            action=action,
            entity_id=record_id,
            path=rel_path,
            payload={
                "full": True,
                "project": payload.project,
                "date": payload.date,
            },
        )
        meta["revision"] = rev

        atomic_write_json(rdir / _META, meta)
        atomic_write_json(rdir / _LINKS, links)

        # --- FTS ---
        fts.index_record(
            record_id,
            path=rel_path,
            project=payload.project,
            date=payload.date,
            name=payload.name,
            texts=_fts_texts(meta, rdir),
        )

    return {
        "id": record_id,
        "revision": rev,
        "action": action,
        "path": rel_path,
        "artifacts": artifacts_meta,
    }


# ---------------------------------------------------------------------------
# PATCH
# ---------------------------------------------------------------------------
def patch_record(
    record_id: str,
    patch: Dict[str, Any],
) -> Dict[str, Any]:
    rdir = find_by_id(record_id)
    if not rdir:
        raise HTTPException(404, f"Record not found: {record_id}")

    with write_lock():
        meta = read_meta(rdir)
        if not meta:
            raise HTTPException(500, "Corrupted record: no _meta.json")

        changed: Dict[str, Any] = {}
        for key, value in patch.items():
            if value is None:
                continue
            if meta.get(key) != value:
                meta[key] = value
                changed[key] = value

        if not changed:
            log.info("PATCH: нет изменений для %s", record_id)
            return {
                "id": record_id,
                "revision": int(meta.get("revision", 0)),
                "changed": [],
            }

        meta["updated_at"] = _now_iso()

        rel = str(rdir.relative_to(settings.records_root)).replace(
            "\\", "/"
        )
        rev = append_change(
            entity="record",
            action="update",
            entity_id=record_id,
            path=rel,
            payload={"changed": list(changed.keys())},
        )
        meta["revision"] = rev

        atomic_write_json(rdir / _META, meta)

        # обновим FTS, если затронули текст
        if {"summary_bb", "name"} & set(changed.keys()):
            fts.index_record(
                record_id,
                path=rel,
                project=meta.get("project", ""),
                date=meta.get("date", ""),
                name=meta.get("name", ""),
                texts=_fts_texts(meta, rdir),
            )

    log.info(
        "PATCH применён: id=%s, полей=%d, revision=%d",
        record_id, len(changed), rev,
    )
    return {
        "id": record_id,
        "revision": rev,
        "changed": list(changed.keys()),
    }


# ---------------------------------------------------------------------------
# Удаление (soft-delete)
# ---------------------------------------------------------------------------
def soft_delete(record_id: str, hard: bool = False) -> Dict[str, Any]:
    rdir = find_by_id(record_id)
    if not rdir:
        raise HTTPException(404, f"Record not found: {record_id}")

    with write_lock():
        if hard:
            rel = str(rdir.relative_to(settings.records_root)).replace(
                "\\", "/"
            )
            shutil.rmtree(rdir)
            fts.remove_record(record_id)
            rev = append_change(
                entity="record",
                action="delete",
                entity_id=record_id,
                path=rel,
                extra={"hard": True},
            )
            log.warning(
                "HARD DELETE: id=%s, path=%s, revision=%d",
                record_id, rel, rev,
            )
            return {"id": record_id, "revision": rev, "hard": True}

        # soft-delete: маркер + changelog
        rel = str(rdir.relative_to(settings.records_root)).replace(
            "\\", "/"
        )
        marker = {
            "deleted_at": _now_iso(),
            "id": record_id,
        }
        atomic_write_json(rdir / _DELETED, marker)

        meta = read_meta(rdir) or {}
        meta["deleted_at"] = marker["deleted_at"]
        meta["updated_at"] = marker["deleted_at"]

        rev = append_change(
            entity="record",
            action="delete",
            entity_id=record_id,
            path=rel,
        )
        meta["revision"] = rev
        atomic_write_json(rdir / _META, meta)

    log.warning(
        "SOFT DELETE: id=%s, path=%s, revision=%d",
        record_id, rel, rev,
    )
    return {"id": record_id, "revision": rev, "hard": False}


# ---------------------------------------------------------------------------
# Ссылки на видео/аудио
# ---------------------------------------------------------------------------
def update_links(
    record_id: str,
    *,
    video_url: Optional[str] = None,
    video_size: Optional[int] = None,
    video_mime: Optional[str] = None,
    video_duration: Optional[float] = None,
) -> Dict[str, Any]:
    rdir = find_by_id(record_id)
    if not rdir:
        raise HTTPException(404, f"Record not found: {record_id}")

    with write_lock():
        links = read_links(rdir) or {}
        video = links.get("video") or {}
        if video_url is not None:
            video["url"] = video_url
        if video_size is not None:
            video["size"] = video_size
        if video_mime is not None:
            video["mime"] = video_mime
        if video_duration is not None:
            video["duration"] = video_duration
        video["updated_at"] = _now_iso()
        links["video"] = video
        links["updated_at"] = _now_iso()

        atomic_write_json(rdir / _LINKS, links)

        rel = str(rdir.relative_to(settings.records_root)).replace(
            "\\", "/"
        )
        rev = append_change(
            entity="record",
            action="update_links",
            entity_id=record_id,
            path=rel,
            payload={"video_url": video.get("url", "")},
        )

    return {"id": record_id, "revision": rev, "video": video}


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------
def _fts_texts(meta: Dict[str, Any], rdir: Path) -> List[str]:
    """Собирает тексты для индексации."""
    texts: List[str] = [
        meta.get("summary_bb", ""),
        meta.get("comment", ""),
        meta.get("prompt", ""),
        meta.get("name", ""),
    ]
    for art in meta.get("artifacts", []) or []:
        kind = art.get("kind", "")
        if kind in ("transcript", "protocol", "manual_protocol"):
            p = rdir / art.get("filename", "")
            if p.is_file() and p.stat().st_size < 2 * 1024 * 1024:
                try:
                    texts.append(p.read_text(
                        encoding="utf-8", errors="ignore"
                    ))
                except Exception as exc:
                    log.warning(
                        "FTS: не удалось прочитать %s: %s", p, exc
                    )
    return texts