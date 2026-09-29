from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

from ..config import settings
from ..models import HealthResponse
from ..revision import get_revision

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    count = 0
    if settings.records_root.is_dir():
        count = sum(1 for _ in settings.records_root.rglob("_meta.json"))
    return HealthResponse(
        status="ok",
        revision=get_revision(),
        records_count=count,
        data_root=str(settings.data_root),
        fts_enabled=settings.fts_enabled,
        time=datetime.now(timezone.utc).isoformat(),
    )