"""Публичный просмотр записи (без авторизации).

Доступ по полному пути + id из _meta.json (проверка совпадения).

  GET /view/{project}/{year}/{month}/{folder_name}
      ?id=<record_id>
      &blocks=video,audio,transcript,protocol,summary

  GET /view/{project}/{year}/{month}/{folder_name}/artifact/{filename}
      ?id=<record_id>

Блоки, которые можно включать через ?blocks=:
  • video       — HTML5 <video> плеер (ссылка из _links.json)
  • audio       — HTML5 <audio> плеер (артефакт kind=audio или _links.json)
  • transcript  — ссылка на файл стенограммы
  • protocol    — текст протокола + кнопка «Скачать»
  • summary     — текст summary_bb
  • all         — все доступные блоки (по умолчанию)

Безопасность:
  • id из URL должен совпадать с id в _meta.json;
  • путь нормализуется через _safe_segment + assert_inside;
  • файлы артефактов отдаются только из папки этой записи.
"""
from __future__ import annotations

import html
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse

from ..atomic import read_json
from ..config import settings
from ..logger import get_logger
from ..paths import _safe_segment, assert_inside

router = APIRouter(prefix="/view", tags=["view"])
log = get_logger(__name__)

_ALL_BLOCKS = {"video", "audio", "transcript", "protocol", "summary"}
_META = "_meta.json"
_LINKS = "_links.json"


# ---------------------------------------------------------------------------
# Разбор параметров
# ---------------------------------------------------------------------------
def _parse_blocks(raw: Optional[str]) -> Set[str]:
    """
    Разбирает ?blocks=video,audio,... в множество.

    Пустое значение или отсутствие → все блоки.
    'all' → все блоки.
    Неизвестные значения игнорируются.
    """
    if not raw:
        return set(_ALL_BLOCKS)
    items = {b.strip().lower() for b in raw.split(",") if b.strip()}
    if not items or "all" in items:
        return set(_ALL_BLOCKS)
    return items & _ALL_BLOCKS


# ---------------------------------------------------------------------------
# Загрузка записи
# ---------------------------------------------------------------------------
def _record_dir(
    project: str, year: str, month: str, folder_name: str,
) -> Path:
    """
    Собирает путь к папке записи с санитизацией сегментов.

    Бросает 404, если папки нет.
    """
    # Та же санитизация, что и в paths.safe_relative_path
    safe = Path(
        _safe_segment(project, max_bytes=100),
        _safe_segment(year, max_bytes=10),
        _safe_segment(month, max_bytes=10),
        _safe_segment(folder_name, max_bytes=200),
    )
    rdir = settings.records_root / safe
    assert_inside(settings.records_root, rdir)

    if not rdir.is_dir():
        raise HTTPException(404, "Record not found")
    return rdir


def _load_record(
    rdir: Path, record_id: str,
) -> Dict[str, Any]:
    """
    Читает _meta.json и проверяет совпадение id.

    Бросает 404, если _meta.json нет, id не совпадает
    или запись помечена deleted.
    """
    meta_path = rdir / _META
    if not meta_path.is_file():
        raise HTTPException(404, "Record not found")

    meta = read_json(meta_path, default=None)
    if not isinstance(meta, dict):
        raise HTTPException(500, "Corrupted record")

    # Защита от подмены пути: id в URL должен совпадать
    # с id в _meta.json.
    if not record_id or meta.get("id") != record_id:
        log.warning(
            "view: id mismatch (url=%r, meta=%r, path=%s)",
            record_id, meta.get("id"), rdir,
        )
        raise HTTPException(404, "Record not found")

    # Soft-deleted записи не показываем публично.
    if meta.get("deleted_at"):
        raise HTTPException(404, "Record not found")

    return meta


# ---------------------------------------------------------------------------
# Хелперы для ссылок
# ---------------------------------------------------------------------------
def _public_artifact_url(
    project: str, year: str, month: str, folder_name: str,
    filename: str, record_id: str,
) -> str:
    """Публичная ссылка на артефакт этой записи."""
    return (
        f"/view/{quote(project)}/{quote(year)}/{quote(month)}/"
        f"{quote(folder_name)}/artifact/{quote(filename)}"
        f"?id={quote(record_id)}"
    )


def _find_artifact(
    meta: Dict[str, Any], kind: str,
) -> Optional[Dict[str, Any]]:
    """Первый артефакт указанного kind."""
    for art in meta.get("artifacts", []) or []:
        if art.get("kind") == kind:
            return art
    return None


def _find_artifacts(
    meta: Dict[str, Any], kinds: Set[str],
) -> List[Dict[str, Any]]:
    return [
        a for a in (meta.get("artifacts", []) or [])
        if a.get("kind") in kinds
    ]


# ---------------------------------------------------------------------------
# HTML-рендеринг
# ---------------------------------------------------------------------------
def _e(value: Any) -> str:
    """HTML-escape."""
    return html.escape(str(value or ""))


_CSS = """
:root {
  --bg: #f6f7f9; --card: #fff; --fg: #1c1e21;
  --muted: #65676b; --border: #dddfe2; --accent: #1877f2;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 24px;
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI",
        Roboto, Helvetica, Arial, sans-serif;
  background: var(--bg); color: var(--fg);
}
.container { max-width: 960px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 0 0 12px; color: var(--muted);
     font-weight: 600; text-transform: uppercase;
     letter-spacing: .04em; }
.card {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 10px; padding: 18px 20px; margin-bottom: 16px;
}
.meta-grid {
  display: grid; grid-template-columns: max-content 1fr;
  gap: 6px 16px; font-size: 14px;
}
.meta-grid dt { color: var(--muted); }
.meta-grid dd { margin: 0; }
.tags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.tag {
  background: #e7f3ff; color: #1877f2; border-radius: 12px;
  padding: 2px 10px; font-size: 12px;
}
video, audio { width: 100%; border-radius: 8px; background: #000; }
audio { background: transparent; }
.btn {
  display: inline-block; padding: 8px 14px; border-radius: 6px;
  background: var(--accent); color: #fff; text-decoration: none;
  font-size: 14px; border: 0; cursor: pointer;
}
.btn:hover { opacity: .9; }
.btn.secondary {
  background: transparent; color: var(--accent);
  border: 1px solid var(--accent);
}
pre.text {
  white-space: pre-wrap; word-wrap: break-word;
  background: #fafbfc; border: 1px solid var(--border);
  border-radius: 8px; padding: 14px; margin: 0;
  font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
  max-height: 520px; overflow: auto;
}
a.file-link { color: var(--accent); text-decoration: none; }
a.file-link:hover { text-decoration: underline; }
.empty { color: var(--muted); font-style: italic; }
.footer {
  text-align: center; color: var(--muted);
  font-size: 12px; margin-top: 24px;
}
"""


def _render_meta_block(meta: Dict[str, Any], rdir: Path) -> str:
    """Заголовок + метаданные записи."""
    name = meta.get("name") or meta.get("folder_name") or "Запись"
    rows: List[str] = []

    def row(label: str, value: Any) -> None:
        if value in (None, "", []):
            return
        rows.append(
            f"<dt>{_e(label)}</dt><dd>{_e(value)}</dd>"
        )

    row("Проект", meta.get("project"))
    row("Дата", meta.get("date"))
    row("Время", meta.get("time"))
    row("Описание", meta.get("description"))
    row("Комментарий", meta.get("comment"))
    row("Источник", meta.get("source"))
    if meta.get("is_scrum"):
        row("Тип", "Scrum")

    tags = meta.get("tags") or []
    tags_html = ""
    if tags:
        chips = "".join(
            f'<span class="tag">{_e(t)}</span>' for t in tags
        )
        tags_html = f'<div class="tags">{chips}</div>'

    return f"""
    <div class="card">
      <h1>{_e(name)}</h1>
      <dl class="meta-grid">{''.join(rows)}</dl>
      {tags_html}
    </div>
    """


def _render_video_block(
    links: Dict[str, Any],
) -> str:
    video = links.get("video") or {}
    url = (video.get("url") or "").strip()
    if not url:
        return ""
    mime = video.get("mime") or ""
    return f"""
    <div class="card">
      <h2>Видео</h2>
      <video controls preload="metadata" src="{_e(url)}"
             {'type="' + _e(mime) + '"' if mime else ''}>
        Ваш браузер не поддерживает видео.
        <a href="{_e(url)}">Скачать</a>
      </video>
    </div>
    """


def _render_audio_block(
    meta: Dict[str, Any],
    project: str, year: str, month: str, folder_name: str,
    record_id: str,
) -> str:
    """
    Аудио: ищем артефакт kind=audio или audio в _links.json.
    """
    # 1. Артефакт kind=audio
    art = _find_artifact(meta, "audio")
    if art:
        src = _public_artifact_url(
            project, year, month, folder_name,
            art["filename"], record_id,
        )
        return f"""
        <div class="card">
          <h2>Аудио</h2>
          <audio controls preload="metadata" src="{_e(src)}">
            Ваш браузер не поддерживает аудио.
            <a href="{_e(src)}">Скачать</a>
          </audio>
        </div>
        """

    # 2. _links.json → audio
    links = read_json(meta.get("_rdir", Path()) / "_links.json", default={}) or {}
    audio = links.get("audio") or {}
    url = (audio.get("url") or "").strip()
    if url:
        return f"""
        <div class="card">
          <h2>Аудио</h2>
          <audio controls preload="metadata" src="{_e(url)}">
            Ваш браузер не поддерживает аудио.
          </audio>
        </div>
        """
    return ""


def _render_transcript_block(
    meta: Dict[str, Any],
    project: str, year: str, month: str, folder_name: str,
    record_id: str,
) -> str:
    arts = _find_artifacts(meta, {"transcript"})
    if not arts:
        return ""
    items = []
    for art in arts:
        fn = art.get("filename", "")
        url = _public_artifact_url(
            project, year, month, folder_name, fn, record_id,
        )
        size = art.get("size", 0)
        items.append(
            f'<div><a class="file-link" href="{_e(url)}" '
            f'target="_blank" rel="noopener">{_e(fn)}</a> '
            f'<span class="empty">({size} Б)</span></div>'
        )
    return f"""
    <div class="card">
      <h2>Стенограмма</h2>
      {''.join(items)}
    </div>
    """


def _render_protocol_block(
    meta: Dict[str, Any],
    rdir: Path,
    project: str, year: str, month: str, folder_name: str,
    record_id: str,
) -> str:
    """
    Протокол: показываем текст + кнопку «Скачать».

    Текст читаем из файла, если размер < 2 МБ и это текст.
    Иначе — только ссылка на скачивание.
    """
    arts = _find_artifacts(
        meta, {"protocol", "manual_protocol"},
    )
    if not arts:
        return ""

    blocks: List[str] = []
    for art in arts:
        fn = art.get("filename", "")
        url = _public_artifact_url(
            project, year, month, folder_name, fn, record_id,
        )
        size = int(art.get("size", 0))
        kind = art.get("kind", "protocol")
        label = "Протокол" if kind == "protocol" else "Ручной протокол"

        text_html = ""
        p = rdir / fn
        if p.is_file() and p.stat().st_size < 2 * 1024 * 1024:
            # Читаем как текст. Если бинарь — не показываем.
            if _is_text_file(p):
                try:
                    content = p.read_text(
                        encoding="utf-8", errors="replace",
                    )
                    text_html = (
                        f'<pre class="text">{_e(content)}</pre>'
                    )
                except OSError:
                    text_html = ""

        blocks.append(f"""
        <div style="margin-bottom:16px">
          <div style="display:flex;justify-content:space-between;
                      align-items:center;margin-bottom:8px">
            <strong>{_e(label)}: {_e(fn)}</strong>
            <a class="btn secondary" href="{_e(url)}"
               download>Скачать</a>
          </div>
          {text_html}
        </div>
        """)

    return f"""
    <div class="card">
      <h2>Протокол</h2>
      {''.join(blocks)}
    </div>
    """


def _render_summary_block(meta: Dict[str, Any]) -> str:
    summary = (meta.get("summary_bb") or "").strip()
    if not summary:
        return ""
    return f"""
    <div class="card">
      <h2>Summary</h2>
      <pre class="text">{_e(summary)}</pre>
    </div>
    """


def _is_text_file(path: Path) -> bool:
    """
    Грубая эвристика: считаем файл текстовым, если в первых
    4 КБ нет нулевых байтов.
    """
    try:
        with open(path, "rb") as f:
            chunk = f.read(4096)
    except OSError:
        return False
    return b"\x00" not in chunk


def _render_page(
    *,
    meta: Dict[str, Any],
    rdir: Path,
    links: Dict[str, Any],
    blocks: Set[str],
    project: str, year: str, month: str, folder_name: str,
    record_id: str,
) -> str:
    parts: List[str] = []
    parts.append(_render_meta_block(meta, rdir))

    if "video" in blocks:
        parts.append(_render_video_block(links))
    if "audio" in blocks:
        parts.append(_render_audio_block(
            meta, project, year, month, folder_name, record_id,
        ))
    if "transcript" in blocks:
        parts.append(_render_transcript_block(
            meta, project, year, month, folder_name, record_id,
        ))
    if "protocol" in blocks:
        parts.append(_render_protocol_block(
            meta, rdir, project, year, month, folder_name, record_id,
        ))
    if "summary" in blocks:
        parts.append(_render_summary_block(meta))

    # Если после фильтрации не осталось ни одного блока кроме meta —
    # покажем подсказку.
    if len(parts) == 1:
        parts.append(
            '<div class="card"><span class="empty">'
            "Нет данных для отображения с выбранными блоками."
            "</span></div>"
        )

    title = _e(meta.get("name") or meta.get("folder_name") or "Запись")

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="container">
{''.join(parts)}
<div class="footer">screc-server · просмотр записи</div>
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Эндпоинты
# ---------------------------------------------------------------------------
@router.get(
    "/{project}/{year}/{month}/{folder_name}",
    response_class=HTMLResponse,
)
async def view_record(
    request: Request,
    project: str,
    year: str,
    month: str,
    folder_name: str,
    id: str = Query(..., min_length=1, description="record_id"),
    blocks: Optional[str] = Query(
        None,
        description=(
            "Список блоков через запятую: "
            "video,audio,transcript,protocol,summary или all"
        ),
    ),
) -> HTMLResponse:
    """
    Публичная HTML-страница записи.

    Без авторизации. Защита — совпадение id из URL с id
    в _meta.json.
    """
    rdir = _record_dir(project, year, month, folder_name)
    meta = _load_record(rdir, id)
    links = read_json(rdir / "_links.json", default={}) or {}

    enabled = _parse_blocks(blocks)
    log.info(
        "view: path=%s/%s/%s/%s, id=%s, blocks=%s",
        project, year, month, folder_name, id,
        ",".join(sorted(enabled)) or "—",
    )

    page = _render_page(
        meta=meta,
        rdir=rdir,
        links=links,
        blocks=enabled,
        project=project,
        year=year,
        month=month,
        folder_name=folder_name,
        record_id=id,
    )
    return HTMLResponse(content=page)


@router.get(
    "/{project}/{year}/{month}/{folder_name}/artifact/{filename}",
)
async def view_artifact(
    project: str,
    year: str,
    month: str,
    folder_name: str,
    filename: str,
    id: str = Query(..., min_length=1, description="record_id"),
    download: bool = Query(
        False, description="Отдать как attachment",
    ),
) -> FileResponse:
    """
    Публичная отдача артефакта записи.

    Защита:
      • id должен совпадать с _meta.json;
      • filename не должен содержать '/' и '\\';
      • итоговый путь проверяется через assert_inside.

    Отдаётся только файл, который перечислен в _meta.json
    (защита от подсовывания чужих имён).
    """
    rdir = _record_dir(project, year, month, folder_name)
    meta = _load_record(rdir, id)

    # Должен быть в списке артефактов.
    known = {
        a.get("filename") for a in (meta.get("artifacts") or [])
    }
    if filename not in known:
        log.warning(
            "view/artifact: filename %r не в списке артефактов "
            "записи %s", filename, id,
        )
        raise HTTPException(404, "Artifact not found")

    if "/" in filename or "\\" in filename or filename in (".", ".."):
        raise HTTPException(400, "Invalid filename")

    path = rdir / filename
    assert_inside(rdir, path)
    if not path.is_file():
        raise HTTPException(404, "Artifact not found")

    log.info(
        "view/artifact: record=%s, file=%s, size=%d, download=%s",
        id, filename, path.stat().st_size, download,
    )
    return FileResponse(
        path,
        filename=filename,
        content_disposition_type="attachment" if download else "inline",
    )