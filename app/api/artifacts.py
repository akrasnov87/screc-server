"""Скачивание и загрузка артефактов."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, PlainTextResponse

from .. import records_service
from ..atomic import atomic_write_bytes, read_json
from ..auth import require_api_key
from ..locks import write_lock
from ..logger import get_logger

router = APIRouter(
    prefix="/records",
    tags=["artifacts"],
    dependencies=[Depends(require_api_key)],
)
log = get_logger(__name__)


def _safe_artifact_path(rdir: Path, filename: str) -> Path:
    """Защита от path traversal в имени артефакта."""
    if "/" in filename or "\\" in filename or filename in (".", ".."):
        raise HTTPException(400, "Invalid filename")
    target = (rdir / filename).resolve()
    if not str(target).startswith(str(rdir.resolve())):
        raise HTTPException(400, "Path traversal detected")
    return target


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


@router.post("/{record_id}/artifacts")
async def upload_artifact(
    record_id: str,
    file: UploadFile = File(...),
    kind: str = "attachment",
) -> dict:
    rdir = records_service.find_by_id(record_id)
    if not rdir:
        raise HTTPException(404, "Record not found")

    content = await file.read()
    filename = Path(file.filename or "file").name
    path = _safe_artifact_path(rdir, filename)

    with write_lock():
        atomic_write_bytes(path, content)
        meta = read_json(rdir / "_meta.json", default={}) or {}
        arts = list(meta.get("artifacts", []) or [])
        # заменяем запись с тем же именем
        arts = [a for a in arts if a.get("filename") != filename]
        import hashlib
        arts.append({
            "kind": kind,
            "filename": filename,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        })
        meta["artifacts"] = arts
        from ..atomic import atomic_write_json
        atomic_write_json(rdir / "_meta.json", meta)

    log.info(
        "Артефакт загружен: record=%s, kind=%s, name=%s, size=%d",
        record_id, kind, filename, len(content),
    )
    return {"id": record_id, "filename": filename, "size": len(content)}


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
        from ..atomic import atomic_write_json
        atomic_write_json(rdir / "_meta.json", meta)

    log.info(
        "Артефакт удалён: record=%s, name=%s", record_id, filename,
    )
    return {"id": record_id, "filename": filename, "deleted": True}


# --- Удобные шорткаты --------------------------------------------------
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