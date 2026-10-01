"""Скачивание и загрузка артефактов.

Поддерживает проверку SHA-256:
  • POST /records/{id}/artifacts — загрузка с опциональным хэшем.
  • HEAD /records/{id}/artifacts/{filename} — проверка, нужно ли
    загружать файл (без передачи содержимого).
  • POST /records/{id}/artifacts/check — пакетная проверка списка
    файлов перед загрузкой.

Изменения:
  • _safe_artifact_path заменён на assert_inside из paths.py —
    единая защита от path traversal.
  • upload_artifact читает тело чанками с проверкой лимита
    (max_artifact_bytes), не накапливая весь файл в память.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

from fastapi import (
    APIRouter, Depends, File, Form, Header, HTTPException,
    Response, UploadFile, status,
)
from fastapi.responses import FileResponse, PlainTextResponse

from .. import records_service
from ..atomic import atomic_write_json, read_json
from ..auth import require_api_key
from ..config import settings
from ..locks import write_lock
from ..logger import get_logger
from ..models import ArtifactCheckRequest, ArtifactCheckResponse
from ..paths import assert_inside

router = APIRouter(
    prefix="/records",
    tags=["artifacts"],
    dependencies=[Depends(require_api_key)],
)
log = get_logger(__name__)


# Размер чанка при чтении загружаемого файла.
_UPLOAD_CHUNK_SIZE = 1024 * 1024  # 1 МБ


def _safe_artifact_path(rdir: Path, filename: str) -> Path:
    """
    Возвращает безопасный путь к артефакту внутри rdir.

    Защита от path traversal:
      • имя файла не должно содержать '/' или '\\';
      • имя не должно быть '.' или '..';
      • итоговый путь проверяется через assert_inside (единая
        защита с records_service).

    filename приходит из path-параметра FastAPI — уже
    URL-декодированный.
    """
    if not filename:
        raise HTTPException(400, "Empty filename")
    if "/" in filename or "\\" in filename:
        raise HTTPException(400, "Invalid filename")
    if filename in (".", ".."):
        raise HTTPException(400, "Invalid filename")

    target = rdir / filename
    assert_inside(rdir, target)
    return target


async def _read_upload_file_with_limit(
    file: UploadFile,
    max_bytes: int,
) -> bytes:
    """
    Читает UploadFile чанками с проверкой лимита.

    Если файл превышает max_bytes — бросает 413, не накапливая
    весь файл в память (в отличие от await file.read()).

    Возвращает содержимое (bytes). Гарантированно ≤ max_bytes.
    """
    chunks: List[bytes] = []
    total = 0

    while True:
        chunk = await file.read(_UPLOAD_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(
                    f"Artifact '{file.filename or 'file'}' too large: "
                    f"{total} > {max_bytes}"
                ),
            )
        chunks.append(chunk)

    return b"".join(chunks)


# ---------------------------------------------------------------------------
# Список артефактов
# ---------------------------------------------------------------------------
@router.get("/{record_id}/artifacts")
async def list_artifacts(record_id: str) -> dict:
    rdir = records_service.find_by_id(record_id)
    if not rdir:
        raise HTTPException(404, "Record not found")
    meta = read_json(rdir / "_meta.json", default={}) or {}
    return {
        "id": record_id,
        "items": meta.get("artifacts", []) or [],
    }


# ---------------------------------------------------------------------------
# Скачивание
# ---------------------------------------------------------------------------
@router.get("/{record_id}/artifacts/{filename}")
async def download_artifact(
    record_id: str,
    filename: str,
    inline: bool = True,
) -> FileResponse:
    rdir = records_service.find_by_id(record_id)
    if not rdir:
        raise HTTPException(404, "Record not found")
    path = _safe_artifact_path(rdir, filename)
    if not path.is_file():
        raise HTTPException(404, f"Artifact not found: {filename}")
    log.info(
        "Скачивание артефакта: record=%s, file=%s, size=%d",
        record_id, filename, path.stat().st_size,
    )
    return FileResponse(
        path,
        filename=filename,
        content_disposition_type="inline" if inline else "attachment",
    )


# ---------------------------------------------------------------------------
# HEAD: проверка «нужно ли загружать файл»
# ---------------------------------------------------------------------------
@router.head("/{record_id}/artifacts/{filename}")
async def head_artifact(
    record_id: str,
    filename: str,
    x_content_sha256: Optional[str] = Header(
        default=None, alias="X-Content-SHA256",
    ),
) -> Response:
    """
    Проверяет, нужно ли загружать файл, БЕЗ передачи содержимого.

    Заголовки ответа:
      • X-Artifact-Skip       — "true" | "false"
      • X-Artifact-SHA256     — хэш существующего файла или ""
      • X-Artifact-Size       — размер существующего файла или 0
      • X-Artifact-Kind       — kind существующего файла или ""

    Коды:
      • 200 — артефакт существует (skip может быть true/false)
      • 404 — артефакта нет, нужно загрузить
    """
    try:
        result = records_service.check_artifact(
            record_id=record_id,
            filename=filename,
            sha256_client=x_content_sha256,
        )
    except HTTPException as exc:
        if exc.status_code == 404:
            log.info(
                "HEAD артефакта: запись не найдена, id=%s", record_id
            )
        raise

    headers = {
        "X-Artifact-Skip": "true" if result["skip"] else "false",
        "X-Artifact-SHA256": result.get("sha256", ""),
        "X-Artifact-Size": str(result.get("size", 0)),
        "X-Artifact-Kind": result.get("kind", ""),
    }

    if not result["exists"]:
        return Response(status_code=404, headers=headers)

    log.debug(
        "HEAD артефакта: record=%s, name=%s, exists=%s, skip=%s, "
        "reason=%s",
        record_id, filename, result["exists"], result["skip"],
        result["reason"],
    )
    return Response(status_code=200, headers=headers)


# ---------------------------------------------------------------------------
# POST: пакетная проверка
# ---------------------------------------------------------------------------
@router.post(
    "/{record_id}/artifacts/check",
    response_model=ArtifactCheckResponse,
)
async def check_artifacts_batch(
    record_id: str,
    body: ArtifactCheckRequest,
) -> ArtifactCheckResponse:
    """
    Пакетная проверка списка файлов перед загрузкой.

    Возвращает для каждого файла skip-флаг и reason.
    Клиент должен отправить только те файлы, у которых skip=false.
    """
    if not body.artifacts:
        return ArtifactCheckResponse(id=record_id, results=[])

    items = [
        {"filename": it.filename, "sha256": it.sha256}
        for it in body.artifacts
    ]
    raw = records_service.check_artifacts_batch(
        record_id=record_id, items=items,
    )
    return ArtifactCheckResponse(**raw)


# ---------------------------------------------------------------------------
# Загрузка одного артефакта
# ---------------------------------------------------------------------------
@router.post("/{record_id}/artifacts")
async def upload_artifact(
    record_id: str,
    file: UploadFile = File(...),
    kind: str = Form("attachment"),
    sha256: Optional[str] = Form(None),
) -> dict:
    """
    Загружает один артефакт в существующую запись.

    Если sha256 передан и совпадает с уже сохранённым файлом
    с таким же именем — файл НЕ перезаписывается, возвращается
    {"skipped": true}.

    Файл читается чанками с проверкой лимита max_artifact_bytes —
    не накапливается в память целиком.
    """
    content = await _read_upload_file_with_limit(
        file, settings.max_artifact_bytes
    )
    filename = Path(file.filename or "file").name

    sha_clean: Optional[str] = None
    if sha256:
        sha_clean = sha256.strip().lower() or None

    return records_service.upload_single_artifact(
        record_id=record_id,
        kind=kind,
        filename=filename,
        content=content,
        sha256_client=sha_clean,
    )


# ---------------------------------------------------------------------------
# Удаление
# ---------------------------------------------------------------------------
@router.delete("/{record_id}/artifacts/{filename}")
async def delete_artifact(record_id: str, filename: str) -> dict:
    rdir = records_service.find_by_id(record_id)
    if not rdir:
        raise HTTPException(404, "Record not found")
    path = _safe_artifact_path(rdir, filename)

    with write_lock():
        if path.is_file():
            path.unlink()
        meta = read_json(rdir / "_meta.json", default={}) or {}
        arts = [
            a for a in (meta.get("artifacts", []) or [])
            if a.get("filename") != filename
        ]
        meta["artifacts"] = arts
        atomic_write_json(rdir / "_meta.json", meta)

    log.info(
        "Артефакт удалён: record=%s, name=%s", record_id, filename,
    )
    return {"id": record_id, "filename": filename, "deleted": True}


# ---------------------------------------------------------------------------
# Удобные шорткаты
# ---------------------------------------------------------------------------
@router.get(
    "/{record_id}/transcript",
    response_class=PlainTextResponse,
)
async def get_transcript(record_id: str) -> PlainTextResponse:
    rdir = records_service.find_by_id(record_id)
    if not rdir:
        raise HTTPException(404, "Record not found")
    meta = read_json(rdir / "_meta.json", default={}) or {}
    for a in meta.get("artifacts", []) or []:
        if a.get("kind") == "transcript":
            p = rdir / a["filename"]
            if p.is_file():
                return PlainTextResponse(
                    p.read_text(encoding="utf-8", errors="ignore"),
                    media_type="text/plain; charset=utf-8",
                )
    raise HTTPException(404, "Transcript not found")


@router.get("/{record_id}/summary", response_class=PlainTextResponse)
async def get_summary(record_id: str) -> PlainTextResponse:
    rdir = records_service.find_by_id(record_id)
    if not rdir:
        raise HTTPException(404, "Record not found")
    meta = read_json(rdir / "_meta.json", default={}) or {}
    summary = (meta.get("summary_bb") or "").strip()
    if not summary:
        raise HTTPException(404, "Summary not found")
    return PlainTextResponse(
        summary, media_type="text/markdown; charset=utf-8"
    )