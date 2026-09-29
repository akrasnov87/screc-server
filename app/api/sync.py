"""Дельта-синхронизация."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from .. import records_service
from ..auth import require_api_key
from ..config import settings
from ..logger import get_logger
from ..models import SyncChange, SyncChangesResponse
from ..revision import get_revision, read_changes

router = APIRouter(
    prefix="/sync",
    tags=["sync"],
    dependencies=[Depends(require_api_key)],
)
log = get_logger(__name__)


@router.get("/changes", response_model=SyncChangesResponse)
async def sync_changes(
    since_revision: int = Query(0, ge=0),
    limit: int = Query(0, ge=0),
) -> SyncChangesResponse:
    """Возвращает изменения с rev > since_revision."""
    if limit <= 0:
        limit = settings.sync_page_size
    limit = min(limit, 1000)

    raw = read_changes(since_revision, limit)

    changes = []
    server_revision = since_revision
    for entry in raw:
        rev = entry.get("rev", 0)
        server_revision = max(server_revision, rev)
        changes.append(SyncChange(**entry))

    has_more = len(raw) == limit
    log.info(
        "sync/changes: since=%d, limit=%d → %d изменений, "
        "server_revision=%d, has_more=%s",
        since_revision, limit, len(changes), server_revision, has_more,
    )
    return SyncChangesResponse(
        server_revision=server_revision,
        has_more=has_more,
        changes=changes,
    )


@router.get("/snapshot")
async def sync_snapshot() -> dict:
    """Полный дамп всех записей. Для первой синхронизации."""
    records = []
    root = settings.records_root
    if root.is_dir():
        for meta_path in sorted(root.rglob("_meta.json")):
            rdir = meta_path.parent
            full = records_service.build_full(rdir)
            if full and not full.artifacts is None:
                records.append(full.model_dump())

    log.info("sync/snapshot: отдано %d записей", len(records))
    return {
        "server_revision": get_revision(),
        "total": len(records),
        "records": records,
    }