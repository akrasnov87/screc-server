"""Навигация по дереву: проекты → годы → месяцы → записи."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException

from ..atomic import read_json
from ..auth import require_api_key
from ..config import settings
from ..logger import get_logger
from ..models import TreeMonth, TreeProject, TreeRecord

router = APIRouter(
    prefix="/tree",
    tags=["tree"],
    dependencies=[Depends(require_api_key)],
)
log = get_logger(__name__)


@router.get("", response_model=List[TreeProject])
async def list_projects() -> List[TreeProject]:
    root = settings.records_root
    if not root.is_dir():
        return []

    result: List[TreeProject] = []
    for project_dir in sorted(root.iterdir()):
        if not project_dir.is_dir() or project_dir.name.startswith("."):
            continue
        years = sorted(
            y.name for y in project_dir.iterdir()
            if y.is_dir() and not y.name.startswith(".")
        )
        # посчитаем записи
        count = 0
        for y in project_dir.iterdir():
            if not y.is_dir():
                continue
            for m in y.iterdir():
                if not m.is_dir():
                    continue
                for rec in m.iterdir():
                    if rec.is_dir() and (rec / "_meta.json").is_file():
                        count += 1
        result.append(
            TreeProject(
                name=project_dir.name,
                years=years,
                records_count=count,
            )
        )
    log.debug("tree: проектов=%d", len(result))
    return result


@router.get("/{project}", response_model=List[str])
async def list_years(project: str) -> List[str]:
    pdir = settings.records_root / project
    if not pdir.is_dir():
        raise HTTPException(404, f"Project not found: {project}")
    return sorted(
        y.name for y in pdir.iterdir()
        if y.is_dir() and not y.name.startswith(".")
    )


@router.get("/{project}/{year}", response_model=List[str])
async def list_months(project: str, year: str) -> List[str]:
    ydir = settings.records_root / project / year
    if not ydir.is_dir():
        raise HTTPException(404, f"Year not found: {year}")
    return sorted(
        m.name for m in ydir.iterdir()
        if m.is_dir() and not m.name.startswith(".")
    )


@router.get("/{project}/{year}/{month}", response_model=List[TreeRecord])
async def list_records(
    project: str,
    year: str,
    month: str,
) -> List[TreeRecord]:
    mdir = settings.records_root / project / year / month
    if not mdir.is_dir():
        raise HTTPException(404, f"Month not found: {month}")

    result: List[TreeRecord] = []
    for rec_dir in sorted(mdir.iterdir()):
        if not rec_dir.is_dir() or rec_dir.name.startswith("."):
            continue
        meta_path = rec_dir / "_meta.json"
        if not meta_path.is_file():
            continue
        meta = read_json(meta_path, default={}) or {}
        if meta.get("deleted_at"):
            continue
        result.append(
            TreeRecord(
                id=meta.get("id", ""),
                folder_name=rec_dir.name,
                name=meta.get("name", ""),
                date=meta.get("date", ""),
                tags=list(meta.get("tags", []) or []),
                has_summary=bool((meta.get("summary_bb") or "").strip()),
                artifacts_count=len(meta.get("artifacts", []) or []),
            )
        )
    log.debug(
        "tree: %s/%s/%s → %d записей",
        project, year, month, len(result),
    )
    return result