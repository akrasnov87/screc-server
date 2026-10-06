"""Публичный просмотр записи (без авторизации).

Доступ по полному пути + id из _meta.json (проверка совпадения).

  GET /view/{project}/{year}/{month}/{folder_name}
      ?id=<record_id>
      &blocks=video,audio,transcript,protocol,summary

  GET /view/{project}/{year}/{month}/{folder_name}/artifact/{filename}
      ?id=<record_id>&download=true|false

Блоки, которые можно включать через ?blocks=:
  • video       — HTML5 <video> плеер (артефакт kind=video
                  или HTTP(S)-ссылка из _links.json)
  • audio       — HTML5 <audio> плеер (артефакт kind=audio
                  или HTTP(S)-ссылка из _links.json)
  • transcript  — ссылки на файлы стенограммы
  • protocol    — текст протокола + кнопки «Открыть»/«Скачать»
  • summary     — текст summary_bb (или артефакт kind=summary)
  • all         — все доступные блоки (по умолчанию)

Особенности:
  • Если в _meta.json артефакт помечен kind='attachment', но имя
    файла явно указывает на тип (manual_protocol.docx, summary.md,
    transcript.txt, ...), применяется эвристика `_effective_kind`.
    Это переходная мера — пока клиент не начнёт слать правильные
    kind. См. комментарий к _NAME_TO_KIND.
  • Если в _links.json лежит file://, а артефакт kind=video есть —
    видео играется через /view/.../artifact/{filename}.
  • Все ссылки на артефакты формируются через единый хелпер
    _public_artifact_url с флагом download.
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
# Канонические kind для блоков
# ---------------------------------------------------------------------------
_PROTOCOL_KINDS = {"protocol", "manual_protocol"}
_TRANSCRIPT_KINDS = {"transcript"}
_AUDIO_KINDS = {"audio"}
_VIDEO_KINDS = {"video"}
_SUMMARY_KINDS = {"summary"}


# ---------------------------------------------------------------------------
# Эвристика kind по имени файла (переходная мера)
# ---------------------------------------------------------------------------
# Применяется ТОЛЬКО если kind == 'attachment' или пустой.
# Нужна, чтобы старые записи, где клиент не размечал kind,
# корректно рендерились на публичной странице.
#
# ВАЖНО: выключить (сделать _NAME_TO_KIND = []) после того, как все
# записи будут перезалиты с правильными kind. Иначе, например,
# attachment protocol_final_v2.docx внезапно станет «протоколом».
_NAME_TO_KIND: list[tuple[str, str]] = [
    ("manual_protocol", "manual_protocol"),
    ("protocol",        "protocol"),
    ("transcript",      "transcript"),
    ("summary",         "summary"),
    ("deepseek_prompt", "deepseek_prompt"),
    ("action_items",    "action_items"),
]


def _effective_kind(art: Dict[str, Any]) -> str:
    """
    Определяет «настоящий» kind артефакта.

    1. Если kind задан и не 'attachment' — возвращаем его.
    2. Если kind == 'attachment' — пытаемся угадать по имени файла.
    3. Иначе — 'attachment'.
    """
    kind = (art.get("kind") or "").strip()
    if kind and kind != "attachment":
        return kind

    fn = (art.get("filename") or "").lower()
    stem = fn.rsplit(".", 1)[0] if "." in fn else fn

    for prefix, mapped in _NAME_TO_KIND:
        if (
            stem == prefix
            or stem.startswith(prefix + "_")
            or stem.startswith(prefix + "-")
            or stem.startswith(prefix + " ")
        ):
            return mapped
    return "attachment"


def _find_artifacts(
    meta: Dict[str, Any], kinds: Set[str],
) -> List[Dict[str, Any]]:
    """Возвращает артефакты, чей эффективный kind ∈ kinds."""
    return [
        a for a in (meta.get("artifacts", []) or [])
        if _effective_kind(a) in kinds
    ]


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


def _load_record(rdir: Path, record_id: str) -> Dict[str, Any]:
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
    *,
    download: bool = False,
) -> str:
    """
    Публичная ссылка на артефакт этой записи.

    download=False → Content-Disposition: inline
    download=True  → Content-Disposition: attachment
    """
    url = (
        f"/view/{quote(project)}/{quote(year)}/{quote(month)}/"
        f"{quote(folder_name)}/artifact/{quote(filename)}"
        f"?id={quote(record_id)}"
    )
    if download:
        url += "&download=true"
    return url


def _is_playable_url(url: str) -> bool:
    """
    Возвращает True, если ссылку на медиа можно отдать в <video>/
    <audio>. Отсекаем file:// — браузер их не откроет, и плеер
    будет пустой.
    """
    if not url:
        return False
    u = url.strip().lower()
    return (
        u.startswith("http://")
        or u.startswith("https://")
        or u.startswith("/")
        or u.startswith("./")
        or u.startswith("../")
    )


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
  --warn-bg: #fff8e1; --warn-fg: #8a6d00;
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
.warn {
  background: var(--warn-bg); color: var(--warn-fg);
  border: 1px solid #f0e0a0; border-radius: 8px;
  padding: 10px 12px; font-size: 13px; margin-bottom: 8px;
}
.footer {
  text-align: center; color: var(--muted);
  font-size: 12px; margin-top: 24px;
}
"""


def _render_meta_block(meta: Dict[str, Any]) -> str:
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


# --- Видео -----------------------------------------------------------------
def _guess_video_mime(fn: str) -> str:
    ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
    return {
        "mp4":  "video/mp4",
        "webm": "video/webm",
        "mkv":  "video/x-matroska",
        "mov":  "video/quicktime",
        "avi":  "video/x-msvideo",
    }.get(ext, "")


def _guess_audio_mime(fn: str) -> str:
    ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
    return {
        "mp3":  "audio/mpeg",
        "m4a":  "audio/mp4",
        "aac":  "audio/aac",
        "ogg":  "audio/ogg",
        "opus": "audio/opus",
        "wav":  "audio/wav",
        "flac": "audio/flac",
    }.get(ext, "")


def _render_video_block(
    meta: Dict[str, Any],
    links: Dict[str, Any],
    project: str, year: str, month: str, folder_name: str,
    record_id: str,
) -> str:
    """
    Видео-блок.

    Приоритет:
      1. HTTP(S)-ссылка из _links.json.
      2. Артефакт kind=video — играем через /view/.../artifact/.
      3. file:// или невалидная ссылка — предупреждение.
    """
    video = links.get("video") or {}
    url = (video.get("url") or "").strip()
    mime = video.get("mime") or ""

    # 1. HTTP(S) из _links.json.
    if url and _is_playable_url(url):
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

    # 2. Артефакт kind=video.
    arts = _find_artifacts(meta, _VIDEO_KINDS)
    if arts:
        fn = arts[0].get("filename", "")
        src = _public_artifact_url(
            project, year, month, folder_name, fn, record_id,
        )
        dl = _public_artifact_url(
            project, year, month, folder_name, fn, record_id,
            download=True,
        )
        if not mime:
            mime = _guess_video_mime(fn)
        return f"""
        <div class="card">
          <h2>Видео</h2>
          <video controls preload="metadata" src="{_e(src)}"
                 {'type="' + _e(mime) + '"' if mime else ''}>
            Ваш браузер не поддерживает видео.
            <a href="{_e(dl)}">Скачать</a>
          </video>
        </div>
        """

    # 3. file:// — предупреждение.
    if url:
        return f"""
        <div class="card">
          <h2>Видео</h2>
          <div class="warn">
            Ссылка на видео не может быть открыта в браузере
            (например, <code>file://</code>). Загрузите видео
            как артефакт <code>kind=video</code> или обновите
            ссылку на HTTP(S).
          </div>
          <div><code>{_e(url)}</code></div>
        </div>
        """
    return ""


# --- Аудио -----------------------------------------------------------------
def _render_audio_block(
    meta: Dict[str, Any],
    links: Dict[str, Any],
    project: str, year: str, month: str, folder_name: str,
    record_id: str,
) -> str:
    # 1. Артефакт kind=audio.
    arts = _find_artifacts(meta, _AUDIO_KINDS)
    if arts:
        art = arts[0]
        fn = art.get("filename", "")
        src = _public_artifact_url(
            project, year, month, folder_name, fn, record_id,
        )
        dl = _public_artifact_url(
            project, year, month, folder_name, fn, record_id,
            download=True,
        )
        mime = _guess_audio_mime(fn)
        return f"""
        <div class="card">
          <h2>Аудио</h2>
          <audio controls preload="metadata" src="{_e(src)}"
                 {'type="' + _e(mime) + '"' if mime else ''}>
            Ваш браузер не поддерживает аудио.
            <a href="{_e(dl)}">Скачать</a>
          </audio>
        </div>
        """

    # 2. _links.json → audio.
    audio = links.get("audio") or {}
    url = (audio.get("url") or "").strip()
    if url and _is_playable_url(url):
        mime = audio.get("mime") or ""
        return f"""
        <div class="card">
          <h2>Аудио</h2>
          <audio controls preload="metadata" src="{_e(url)}"
                 {'type="' + _e(mime) + '"' if mime else ''}>
            Ваш браузер не поддерживает аудио.
          </audio>
        </div>
        """
    if url and not _is_playable_url(url):
        return f"""
        <div class="card">
          <h2>Аудио</h2>
          <div class="warn">
            Ссылка на аудио не может быть открыта в браузере
            (например, <code>file://</code>).
          </div>
          <div><code>{_e(url)}</code></div>
        </div>
        """
    return ""


# --- Стенограмма -----------------------------------------------------------
def _render_transcript_block(
    meta: Dict[str, Any],
    project: str, year: str, month: str, folder_name: str,
    record_id: str,
) -> str:
    arts = _find_artifacts(meta, _TRANSCRIPT_KINDS)
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


# --- Протокол --------------------------------------------------------------
def _render_protocol_block(
    meta: Dict[str, Any],
    rdir: Path,
    project: str, year: str, month: str, folder_name: str,
    record_id: str,
) -> str:
    """
    Протокол: показываем текст (если текстовый) + кнопки
    «Открыть» (inline) и «Скачать» (attachment).
    """
    arts = _find_artifacts(meta, _PROTOCOL_KINDS)
    if not arts:
        return ""

    blocks: List[str] = []
    for art in arts:
        fn = art.get("filename", "")
        inline_url = _public_artifact_url(
            project, year, month, folder_name, fn, record_id,
            download=False,
        )
        download_url = _public_artifact_url(
            project, year, month, folder_name, fn, record_id,
            download=True,
        )
        kind = _effective_kind(art)
        label = "Протокол" if kind == "protocol" else "Ручной протокол"

        text_html = ""
        p = rdir / fn
        if p.is_file() and p.stat().st_size < 2 * 1024 * 1024:
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
                      align-items:center;margin-bottom:8px;
                      gap:8px;flex-wrap:wrap">
            <strong>{_e(label)}: {_e(fn)}</strong>
            <span>
              <a class="btn secondary" href="{_e(inline_url)}"
                 target="_blank" rel="noopener">Открыть</a>
              <a class="btn" href="{_e(download_url)}"
                 download>Скачать</a>
            </span>
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


# --- Summary ---------------------------------------------------------------
def _render_summary_block(
    meta: Dict[str, Any], rdir: Path,
) -> str:
    """
    Summary: приоритет — summary_bb из _meta.json.
    Фолбэк — артефакт kind=summary (текстовый, < 2 МБ).
    """
    summary = (meta.get("summary_bb") or "").strip()

    if not summary:
        for art in _find_artifacts(meta, _SUMMARY_KINDS):
            fn = art.get("filename", "")
            p = rdir / fn
            if not p.is_file():
                continue
            if p.stat().st_size >= 2 * 1024 * 1024:
                continue
            if not _is_text_file(p):
                continue
            try:
                summary = p.read_text(
                    encoding="utf-8", errors="replace",
                ).strip()
                if summary:
                    break
            except OSError:
                pass

    if not summary:
        return ""
    return f"""
    <div class="card">
      <h2>Summary</h2>
      <pre class="text">{_e(summary)}</pre>
    </div>
    """


# --- Утилиты ---------------------------------------------------------------
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


# --- Сборка страницы -------------------------------------------------------
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
    parts.append(_render_meta_block(meta))

    if "video" in blocks:
        parts.append(_render_video_block(
            meta, links,
            project, year, month, folder_name, record_id,
        ))
    if "audio" in blocks:
        parts.append(_render_audio_block(
            meta, links,
            project, year, month, folder_name, record_id,
        ))
    if "transcript" in blocks:
        parts.append(_render_transcript_block(
            meta, project, year, month, folder_name, record_id,
        ))
    if "protocol" in blocks:
        parts.append(_render_protocol_block(
            meta, rdir,
            project, year, month, folder_name, record_id,
        ))
    if "summary" in blocks:
        parts.append(_render_summary_block(meta, rdir))

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
    links = read_json(rdir / _LINKS, default={}) or {}

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
      • filename должен быть в списке артефактов _meta.json;
      • filename не должен содержать '/' и '\\';
      • итоговый путь проверяется через assert_inside.
    """
    rdir = _record_dir(project, year, month, folder_name)
    meta = _load_record(rdir, id)

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