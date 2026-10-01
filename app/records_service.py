"""Бизнес-логика записей: создание, обновление, чтение, удаление.

Поддерживает проверку SHA-256 при загрузке артефактов:

  • Если клиент передал sha256 и он совпадает с уже сохранённым
    файлом с таким же именем — файл НЕ перезаписывается,
    возвращается признак skipped.
  • Если sha256 не передан — файл перезаписывается принудительно
    (обратная совместимость с клиентами без хэша).
  • Если sha256 передан, но не совпадает — файл перезаписывается.

Серверный хэш (посчитанный из фактического содержимого) всегда
является источником истины и сохраняется в _meta.json.

Имена файлов от клиентов могут приходить percent-encoded
(например, %D0%9B%D0%AD.xlsx) — aiohttp и некоторые другие
HTTP-клиенты кодируют не-ASCII в Content-Disposition. Функция
_decode_upload_filename декодирует их обратно в Unicode,
чтобы файлы на диске сохранялись с оригинальными именами.

Изменения:
  • find_by_id теперь использует индекс records_index.json
    (id → relative_path) вместо rglob по всему дереву.
    Если id нет в индексе — fallback на старый rglob с
    восстановлением индекса.
  • create_or_update / soft_delete (hard) / patch_record
    (при первичном создании) обновляют индекс.
  • check_artifact проверяет наличие файла на диске
    (path.is_file()), а не только запись в _meta.json.
  • Добавлен флаг sync_ready — признак «готово к синхронизации».
    Сохраняется в _meta.json, читается build_full, может
    меняться через PATCH /records/{id}.
"""
from __future__ import annotations

import hashlib
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

from fastapi import HTTPException

from . import fts
from . import records_index
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


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_sha256(value: Optional[str]) -> Optional[str]:
    """
    Приводит хэш к каноническому виду: lower-case, strip.
    Пустую строку превращает в None.
    """
    if value is None:
        return None
    s = value.strip().lower()
    return s or None


def _decode_upload_filename(raw: str) -> str:
    """
    Декодирует percent-encoding в имени файла от клиента.

    aiohttp (и некоторые другие HTTP-клиенты) кодируют не-ASCII
    имена в Content-Disposition по RFC 3986. starlette/FastAPI
    отдают filename как есть, поэтому нужно декодировать вручную.
    """
    if not raw:
        return raw

    decoded = raw
    for _ in range(3):
        try:
            new_val = unquote(decoded, errors="replace")
        except Exception:
            break
        if new_val == decoded:
            break
        decoded = new_val

    return Path(decoded).name


def _relative_path(rdir: Path) -> str:
    """Относительный путь rdir от records_root, с /."""
    return str(rdir.relative_to(settings.records_root)).replace(
        "\\", "/"
    )


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
        sync_ready=bool(meta.get("sync_ready", False)),
        artifacts=[
            ArtifactInfo(**a) for a in meta.get("artifacts", []) or []
        ],
        video=links.get("video"),
    )


def find_by_id(record_id: str) -> Optional[Path]:
    """
    Ищет папку записи по id.

    Основной путь — индекс records_index.json (O(1)).
    Если id в индексе нет — fallback на обход дерева
    (на случай рассинхрона) с восстановлением индекса.
    """
    if not record_id:
        return None
    if not settings.records_root.is_dir():
        return None

    # --- Быстрый путь: индекс ---
    rel = records_index.get_path(record_id)
    if rel:
        candidate = settings.records_root / rel
        if (candidate / _META).is_file():
            return candidate
        log.warning(
            "Индекс указывает на несуществующую папку: %s "
            "(record_id=%s) — пересоберём",
            candidate, record_id,
        )

    # --- Медленный путь: обход дерева (fallback) ---
    found: Optional[Path] = None
    for meta_path in settings.records_root.rglob(_META):
        meta = read_json(meta_path, default={}) or {}
        if meta.get("id") == record_id:
            found = meta_path.parent
            break

    if found is not None:
        try:
            with write_lock():
                records_index.add(record_id, _relative_path(found))
        except Exception as exc:
            log.warning(
                "Не удалось обновить индекс после fallback: %s", exc
            )

    return found


# ---------------------------------------------------------------------------
# Проверка хэша
# ---------------------------------------------------------------------------
def _find_artifact_by_filename(
    meta: Dict[str, Any], filename: str,
) -> Optional[Dict[str, Any]]:
    """Ищет артефакт по имени в _meta.json."""
    for art in meta.get("artifacts", []) or []:
        if art.get("filename") == filename:
            return art
    return None


def _should_skip_upload(
    *,
    existing_meta: Optional[Dict[str, Any]],
    filename: str,
    sha256_client: Optional[str],
) -> Tuple[bool, str]:
    """
    Решает, можно ли пропустить запись файла.

    Возвращает (skip, reason):
      • skip=True  — файл уже есть с таким хэшем, перезапись не нужна;
      • skip=False — нужно записать файл (причина в reason).
    """
    if not sha256_client:
        return False, "no_sha256_provided"

    if not existing_meta:
        return False, "record_new"

    existing = _find_artifact_by_filename(existing_meta, filename)
    if not existing:
        return False, "artifact_absent"

    existing_sha = (existing.get("sha256") or "").lower()
    if existing_sha and existing_sha == sha256_client:
        return True, "sha256_match"

    return False, "sha256_mismatch"


# ---------------------------------------------------------------------------
# Создание / обновление записи
# ---------------------------------------------------------------------------
async def create_or_update(
    *,
    payload: RecordPayload,
    files: List[Tuple[str, str, bytes, Optional[str]]],
) -> Dict[str, Any]:
    """
    Создаёт или обновляет запись. Идемпотентно по (path).

    files: список кортежей (kind, filename, content, sha256_client).

    Если sha256_client задан и совпадает с уже сохранённым файлом
    с таким же именем — файл НЕ перезаписывается.
    """
    rdir = record_dir(
        payload.project, payload.year,
        payload.month, payload.folder_name,
    )
    assert_inside(settings.records_root, rdir)
    rel_path = _relative_path(rdir)

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
        artifacts_meta: List[Dict[str, Any]] = list(
            (existing_meta or {}).get("artifacts", []) or []
        )

        skipped_artifacts: List[Dict[str, Any]] = []
        uploaded_artifacts: List[Dict[str, Any]] = []

        for kind, filename, content, sha256_raw in files:
            safe_name = _decode_upload_filename(filename)
            if not safe_name:
                raise HTTPException(400, "Empty artifact filename")
            dest = rdir / safe_name
            assert_inside(rdir, dest)

            sha_client = _normalize_sha256(sha256_raw)

            skip, reason = _should_skip_upload(
                existing_meta=existing_meta,
                filename=safe_name,
                sha256_client=sha_client,
            )

            if skip:
                existing_art = _find_artifact_by_filename(
                    existing_meta or {}, safe_name
                ) or {}
                merged = dict(existing_art)
                merged["kind"] = kind

                artifacts_meta = [
                    a for a in artifacts_meta
                    if a.get("filename") != safe_name
                ]
                artifacts_meta.append(merged)
                skipped_artifacts.append(merged)

                log.info(
                    "Артефакт пропущен (sha256 совпал): "
                    "record=%s, kind=%s, name=%s, sha=%s",
                    record_id, kind, safe_name,
                    (sha_client or "")[:12],
                )
                continue

            # --- фактическая запись файла ---
            atomic_write_bytes(dest, content)
            server_sha = _sha256(content)

            new_art = {
                "kind": kind,
                "filename": safe_name,
                "size": len(content),
                "sha256": server_sha,
            }

            artifacts_meta = [
                a for a in artifacts_meta
                if a.get("filename") != safe_name
            ]
            artifacts_meta.append(new_art)
            uploaded_artifacts.append(new_art)

            log.info(
                "Артефакт сохранён: record=%s, kind=%s, name=%s, "
                "size=%d, reason=%s, sha=%s",
                record_id, kind, safe_name, len(content), reason,
                server_sha[:12],
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

            # --- Флаг готовности к синхронизации ---
            "sync_ready": bool(payload.sync_ready),

            "artifacts": artifacts_meta,
            "revision": 0,
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

        # --- changelog ---
        rev = append_change(
            entity="record",
            action=action,
            entity_id=record_id,
            path=rel_path,
            payload={
                "full": True,
                "project": payload.project,
                "date": payload.date,
                "sync_ready": bool(payload.sync_ready),
                "skipped_artifacts": [
                    a.get("filename") for a in skipped_artifacts
                ],
                "uploaded_artifacts": [
                    a.get("filename") for a in uploaded_artifacts
                ],
            },
        )
        meta["revision"] = rev

        atomic_write_json(rdir / _META, meta)
        atomic_write_json(rdir / _LINKS, links)

        # --- индекс id → path ---
        records_index.add(record_id, rel_path)

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
        "skipped_artifacts": skipped_artifacts,
        "uploaded_artifacts": uploaded_artifacts,
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

        rel = _relative_path(rdir)
        rev = append_change(
            entity="record",
            action="update",
            entity_id=record_id,
            path=rel,
            payload={"changed": list(changed.keys())},
        )
        meta["revision"] = rev

        atomic_write_json(rdir / _META, meta)

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
            rel = _relative_path(rdir)
            shutil.rmtree(rdir)
            fts.remove_record(record_id)
            records_index.remove(record_id)
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
        rel = _relative_path(rdir)
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

        rel = _relative_path(rdir)
        rev = append_change(
            entity="record",
            action="update_links",
            entity_id=record_id,
            path=rel,
            payload={"video_url": video.get("url", "")},
        )

    return {"id": record_id, "revision": rev, "video": video}


# ---------------------------------------------------------------------------
# Загрузка одного артефакта в существующую запись
# ---------------------------------------------------------------------------
def upload_single_artifact(
    *,
    record_id: str,
    kind: str,
    filename: str,
    content: bytes,
    sha256_client: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Загружает один артефакт в существующую запись.

    Если sha256_client задан и совпадает с уже сохранённым файлом
    с таким же именем — файл НЕ перезаписывается, возвращается
    {"skipped": True}.
    """
    rdir = find_by_id(record_id)
    if not rdir:
        raise HTTPException(404, f"Record not found: {record_id}")

    safe_name = _decode_upload_filename(filename)
    if not safe_name:
        raise HTTPException(400, "Empty artifact filename")
    dest = rdir / safe_name
    assert_inside(rdir, dest)

    sha_client = _normalize_sha256(sha256_client)

    with write_lock():
        meta = read_meta(rdir) or {}
        skip, reason = _should_skip_upload(
            existing_meta=meta,
            filename=safe_name,
            sha256_client=sha_client,
        )

        if skip:
            existing = _find_artifact_by_filename(meta, safe_name) or {}
            log.info(
                "Артефакт пропущен (sha256 совпал): "
                "record=%s, name=%s, sha=%s",
                record_id, safe_name,
                (sha_client or "")[:12],
            )
            return {
                "id": record_id,
                "filename": safe_name,
                "size": int(existing.get("size", 0)),
                "sha256": existing.get("sha256", ""),
                "skipped": True,
                "reason": reason,
            }

        # --- фактическая запись ---
        atomic_write_bytes(dest, content)
        server_sha = _sha256(content)

        new_art = {
            "kind": kind,
            "filename": safe_name,
            "size": len(content),
            "sha256": server_sha,
        }
        arts = [
            a for a in (meta.get("artifacts", []) or [])
            if a.get("filename") != safe_name
        ]
        arts.append(new_art)
        meta["artifacts"] = arts
        meta["updated_at"] = _now_iso()

        rel = _relative_path(rdir)
        rev = append_change(
            entity="record",
            action="artifact_upload",
            entity_id=record_id,
            path=rel,
            payload={
                "filename": safe_name,
                "kind": kind,
                "size": len(content),
                "sha256": server_sha,
                "reason": reason,
            },
        )
        meta["revision"] = rev

        atomic_write_json(rdir / _META, meta)

    log.info(
        "Артефакт загружен: record=%s, kind=%s, name=%s, size=%d, "
        "reason=%s, sha=%s",
        record_id, kind, safe_name, len(content), reason,
        server_sha[:12],
    )
    return {
        "id": record_id,
        "filename": safe_name,
        "size": len(content),
        "sha256": server_sha,
        "skipped": False,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Проверка артефактов БЕЗ загрузки
# ---------------------------------------------------------------------------
def check_artifact(
    *,
    record_id: str,
    filename: str,
    sha256_client: Optional[str] = None,
) -> Dict[str, Any]:
    rdir = find_by_id(record_id)
    if not rdir:
        raise HTTPException(404, f"Record not found: {record_id}")

    safe_name = _decode_upload_filename(filename)
    if not safe_name:
        raise HTTPException(400, "Empty artifact filename")

    meta = read_meta(rdir) or {}
    existing = _find_artifact_by_filename(meta, safe_name)

    if not existing:
        return {
            "id": record_id,
            "filename": safe_name,
            "exists": False,
            "skip": False,
            "reason": "artifact_absent",
            "sha256": "",
            "size": 0,
            "kind": "",
        }

    artifact_path = rdir / safe_name
    if not artifact_path.is_file():
        log.warning(
            "check_artifact: запись есть в _meta.json, "
            "но файл отсутствует на диске: %s "
            "(record=%s, name=%s)",
            artifact_path, record_id, safe_name,
        )
        return {
            "id": record_id,
            "filename": safe_name,
            "exists": False,
            "skip": False,
            "reason": "file_missing_on_disk",
            "sha256": "",
            "size": 0,
            "kind": "",
        }

    existing_sha = (existing.get("sha256") or "").lower()
    client_sha = _normalize_sha256(sha256_client)

    if client_sha and client_sha == existing_sha:
        skip, reason = True, "sha256_match"
    elif client_sha:
        skip, reason = False, "sha256_mismatch"
    else:
        skip, reason = False, "no_sha256_provided"

    return {
        "id": record_id,
        "filename": safe_name,
        "exists": True,
        "skip": skip,
        "reason": reason,
        "sha256": existing_sha,
        "size": int(existing.get("size", 0)),
        "kind": existing.get("kind", ""),
    }


def check_artifacts_batch(
    *,
    record_id: str,
    items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    if not items:
        return {"id": record_id, "results": []}

    rdir = find_by_id(record_id)
    if not rdir:
        raise HTTPException(404, f"Record not found: {record_id}")

    meta = read_meta(rdir) or {}

    results: List[Dict[str, Any]] = []
    for it in items:
        raw_name = it.get("filename", "")
        safe_name = _decode_upload_filename(raw_name)
        sha = _normalize_sha256(it.get("sha256"))

        if not safe_name:
            results.append({
                "filename": raw_name,
                "sha256": "",
                "skip": False,
                "reason": "invalid_filename",
                "size": 0,
                "kind": "",
            })
            continue

        existing = _find_artifact_by_filename(meta, safe_name)

        if not existing:
            results.append({
                "filename": safe_name,
                "sha256": "",
                "skip": False,
                "reason": "artifact_absent",
                "size": 0,
                "kind": "",
            })
            continue

        if not (rdir / safe_name).is_file():
            log.warning(
                "check_artifacts_batch: файл отсутствует на диске: "
                "%s (record=%s)", safe_name, record_id,
            )
            results.append({
                "filename": safe_name,
                "sha256": "",
                "skip": False,
                "reason": "file_missing_on_disk",
                "size": 0,
                "kind": "",
            })
            continue

        existing_sha = (existing.get("sha256") or "").lower()

        if sha and sha == existing_sha:
            skip, reason = True, "sha256_match"
        elif sha:
            skip, reason = False, "sha256_mismatch"
        else:
            skip, reason = False, "no_sha256_provided"

        results.append({
            "filename": safe_name,
            "sha256": existing_sha,
            "skip": skip,
            "reason": reason,
            "size": int(existing.get("size", 0)),
            "kind": existing.get("kind", ""),
        })

    skipped = sum(1 for r in results if r["skip"])
    log.info(
        "Пакетная проверка артефактов: record=%s, файлов=%d, "
        "пропустить=%d",
        record_id, len(results), skipped,
    )
    return {"id": record_id, "results": results}


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