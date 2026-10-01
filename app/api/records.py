"""CRUD записей.

Изменения:
  • Загрузка файлов — чанками с проверкой лимита
    max_artifact_bytes (не накапливаем гигантский файл в память).
"""
from __future__ import annotations

import json
from typing import List, Optional

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Query, UploadFile,
    status,
)

from .. import fts, records_service
from ..auth import require_api_key
from ..config import settings
from ..logger import get_logger
from ..models import (
    RecordFull, RecordPatch, RecordPayload, RecordResponse,
)

router = APIRouter(
    prefix="/records",
    tags=["records"],
    dependencies=[Depends(require_api_key)],
)
log = get_logger(__name__)


# Размер чанка при чтении загружаемого файла.
_UPLOAD_CHUNK_SIZE = 1024 * 1024  # 1 МБ


async def _read_upload_chunked(
    f: UploadFile,
    max_bytes: int,
) -> bytes:
    """
    Читает UploadFile чанками с проверкой лимита.

    Если файл превышает max_bytes — бросает 413, не накапливая
    весь файл в память.
    """
    chunks: List[bytes] = []
    total = 0

    while True:
        chunk = await f.read(_UPLOAD_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(
                    f"Artifact '{f.filename or 'file'}' too large: "
                    f"{total} > {max_bytes}"
                ),
            )
        chunks.append(chunk)

    return b"".join(chunks)


@router.post("", response_model=RecordResponse)
async def create_or_update_record(
    payload: str = Form(..., description="JSON с метаданными"),
    files: List[UploadFile] = File(default=[]),
    kinds: List[str] = Form(default=[]),
    # Параллельный массив хэшей. Может быть пустым, короче массива
    # files или такой же длины. Если для файла хэш не задан — сервер
    # перезаписывает его принудительно.
    sha256: List[str] = Form(default=[]),
) -> RecordResponse:
    """
    Создаёт или обновляет запись.

    payload — JSON-строка с RecordPayload.
    files — «лёгкие» файлы (transcript, protocol, attachments).
    kinds — параллельный массив типов для files.
    sha256 — параллельный массив хэшей (опционально, для
             пропуска повторной загрузки одинаковых файлов).
    """
    raw = payload.encode("utf-8")
    if len(raw) > settings.max_payload_bytes:
        raise HTTPException(
            413,
            f"Payload too large: {len(raw)} > "
            f"{settings.max_payload_bytes}",
        )

    try:
        data = RecordPayload.model_validate_json(payload)
    except Exception as exc:
        log.warning("Некорректный payload: %s", exc)
        raise HTTPException(400, f"Invalid payload: {exc}")

    if kinds and len(kinds) != len(files):
        raise HTTPException(
            400,
            f"kinds length ({len(kinds)}) != files length "
            f"({len(files)})",
        )

    if sha256 and len(sha256) != len(files):
        raise HTTPException(
            400,
            f"sha256 length ({len(sha256)}) != files length "
            f"({len(files)})",
        )

    parsed_files: List[tuple] = []
    for i, f in enumerate(files):
        # Читаем чанками с проверкой лимита — не накапливаем
        # весь файл в память до проверки.
        content = await _read_upload_chunked(
            f, settings.max_artifact_bytes
        )
        kind = kinds[i] if i < len(kinds) else "attachment"
        sha_i = sha256[i].strip() if i < len(sha256) else None
        if sha_i == "":
            sha_i = None
        parsed_files.append(
            (kind, f.filename or f"file_{i}", content, sha_i)
        )

    result = await records_service.create_or_update(
        payload=data, files=parsed_files,
    )
    log.info(
        "Запись %s: id=%s, revision=%d, файлов=%d, "
        "пропущено=%d, загружено=%d",
        result["action"], result["id"], result["revision"],
        len(parsed_files),
        len(result.get("skipped_artifacts", [])),
        len(result.get("uploaded_artifacts", [])),
    )
    return RecordResponse(**result)


@router.get("/{record_id}", response_model=RecordFull)
async def get_record(record_id: str) -> RecordFull:
    rdir = records_service.find_by_id(record_id)
    if not rdir:
        raise HTTPException(404, f"Record not found: {record_id}")
    full = records_service.build_full(rdir)
    if not full:
        raise HTTPException(500, "Corrupted record")
    return full


@router.patch("/{record_id}")
async def patch_record(
    record_id: str,
    patch: RecordPatch,
) -> dict:
    data = patch.model_dump(exclude_none=True)
    return records_service.patch_record(record_id, data)


@router.delete("/{record_id}")
async def delete_record(
    record_id: str,
    hard: bool = Query(False, description="Удалить физически"),
) -> dict:
    return records_service.soft_delete(record_id, hard=hard)


# --- Ссылки на видео/аудио ---------------------------------------------
@router.get("/{record_id}/video-url")
async def get_video_url(record_id: str) -> dict:
    rdir = records_service.find_by_id(record_id)
    if not rdir:
        raise HTTPException(404, f"Record not found: {record_id}")
    links = records_service.read_links(rdir) or {}
    video = links.get("video")
    if not video:
        return {"url": "", "available": False}
    return {
        "url": video.get("url", ""),
        "size": video.get("size", 0),
        "mime": video.get("mime", ""),
        "duration": video.get("duration", 0),
        "available": bool(video.get("url")),
    }


@router.put("/{record_id}/video-url")
async def set_video_url(
    record_id: str,
    body: dict,
) -> dict:
    """
    Обновляет ссылку на видео. Полезно, когда клиент B хочет
    «прикрепить» локальный путь к записи, скачанной с сервера.
    """
    return records_service.update_links(
        record_id,
        video_url=body.get("url"),
        video_size=body.get("size"),
        video_mime=body.get("mime"),
        video_duration=body.get("duration"),
    )


# --- Поиск -------------------------------------------------------------
@router.get("/_/search")
async def search(
    q: str = Query(..., min_length=1),
    project: str = Query(""),
    limit: int = Query(50, ge=1, le=500),
) -> dict:
    results = fts.search(q, project=project, limit=limit)
    return {"query": q, "total": len(results), "items": results}