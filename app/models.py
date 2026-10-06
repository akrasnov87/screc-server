"""Pydantic-схемы запросов и ответов."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Канонические типы артефактов
# ---------------------------------------------------------------------------
# Клиент обязан использовать эти значения в поле `kind`.
# Публичный просмотр (/view/**) рендерит блоки именно по этому полю.
#
# Если нужно добавить новый тип — добавить сюда и в view.py
# (см. _PROTOCOL_KINDS, _TRANSCRIPT_KINDS, _AUDIO_KINDS, ...).
ARTIFACT_KINDS: frozenset[str] = frozenset({
    # медиа
    "video",
    "audio",
    # текст
    "transcript",          # стенограмма
    "protocol",            # автоматический протокол
    "manual_protocol",     # ручной протокол
    "summary",             # summary (файлом, если не пишется в _meta.json)
    "deepseek_prompt",     # промпт для DeepSeek
    "action_items",        # список действий
    # прочее
    "attachment",
})


# ---------------------------------------------------------------------------
# Публикация записи
# ---------------------------------------------------------------------------
class RecordPayload(BaseModel):
    # Если id не задан — сервер сгенерирует UUID.
    id: Optional[str] = None

    project: str = Field(min_length=1, max_length=200)
    year: str = Field(min_length=4, max_length=4)
    month: str = Field(min_length=1, max_length=2)
    folder_name: str = Field(min_length=1, max_length=250)

    name: str = ""
    description: str = ""
    comment: str = ""
    source: str = "record"
    is_scrum: bool = False

    date: str = ""
    time: str = ""

    tags: List[str] = Field(default_factory=list)

    summary_bb: str = ""
    prompt: str = ""
    prompt_name: str = ""
    prompt_edited: bool = False
    name_template: str = ""
    name_abbr: str = ""

    generate_summary: bool = False
    generate_deepseek_prompt: bool = True
    include_name_in_prompt: bool = True
    include_project_in_prompt: bool = True
    include_comment_in_prompt: bool = True
    include_tags_in_prompt: bool = True

    # --- Флаг «готово к синхронизации» ---
    # Пока False — запись считается черновиком.
    sync_ready: bool = False

    video_url: str = ""
    video_size: int = 0
    video_mime: str = ""
    video_duration: float = 0.0


# ---------------------------------------------------------------------------
# Артефакты
# ---------------------------------------------------------------------------
class ArtifactInfo(BaseModel):
    kind: str
    filename: str
    size: int = 0
    sha256: str = ""

    @field_validator("kind")
    @classmethod
    def _check_kind(cls, v: str) -> str:
        """
        Разрешаем только канонические kind.

        Если kind пустой — трактуем как 'attachment'.
        Неизвестный kind → ошибка валидации (400 при запросе).
        """
        v = (v or "").strip()
        if not v:
            return "attachment"
        if v not in ARTIFACT_KINDS:
            raise ValueError(
                f"Unknown artifact kind: {v!r}. "
                f"Allowed: {sorted(ARTIFACT_KINDS)}"
            )
        return v


# ---------------------------------------------------------------------------
# Ответы
# ---------------------------------------------------------------------------
class RecordResponse(BaseModel):
    id: str
    revision: int
    action: str          # create / update
    path: str
    artifacts: List[ArtifactInfo]
    skipped_artifacts: List[ArtifactInfo] = Field(default_factory=list)
    uploaded_artifacts: List[ArtifactInfo] = Field(default_factory=list)


class RecordFull(BaseModel):
    id: str
    revision: int
    project: str
    year: str
    month: str
    folder_name: str
    path: str

    name: str
    description: str
    comment: str
    source: str
    is_scrum: bool
    date: str
    time: str
    created_at: str
    updated_at: str
    tags: List[str]

    summary_bb: str
    prompt: str
    prompt_name: str
    prompt_edited: bool
    name_template: str
    name_abbr: str

    generate_summary: bool
    generate_deepseek_prompt: bool
    include_name_in_prompt: bool
    include_project_in_prompt: bool
    include_comment_in_prompt: bool
    include_tags_in_prompt: bool

    # --- Флаг «готово к синхронизации» ---
    sync_ready: bool = False

    artifacts: List[ArtifactInfo] = Field(default_factory=list)
    video: Optional[Dict[str, Any]] = None


# --- PATCH --------------------------------------------------------------
class RecordPatch(BaseModel):
    summary_bb: Optional[str] = None
    comment: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[List[str]] = None
    is_scrum: Optional[bool] = None
    generate_summary: Optional[bool] = None
    generate_deepseek_prompt: Optional[bool] = None
    include_name_in_prompt: Optional[bool] = None
    include_project_in_prompt: Optional[bool] = None
    include_comment_in_prompt: Optional[bool] = None
    include_tags_in_prompt: Optional[bool] = None
    prompt: Optional[str] = None
    prompt_name: Optional[str] = None
    prompt_edited: Optional[bool] = None
    # --- Флаг «готово к синхронизации» ---
    sync_ready: Optional[bool] = None
    video_url: Optional[str] = None
    video_size: Optional[int] = None
    video_mime: Optional[str] = None


# --- Дерево -------------------------------------------------------------
class TreeProject(BaseModel):
    name: str
    years: List[str] = Field(default_factory=list)
    records_count: int = 0


class TreeMonth(BaseModel):
    month: str
    records_count: int = 0


class TreeRecord(BaseModel):
    id: str
    folder_name: str
    name: str
    date: str
    tags: List[str] = Field(default_factory=list)
    has_summary: bool = False
    artifacts_count: int = 0
    # --- Флаг «готово к синхронизации» ---
    sync_ready: bool = False


# --- Синхронизация ------------------------------------------------------
class SyncChange(BaseModel):
    rev: int
    ts: str
    entity: str
    action: str
    id: str
    path: str
    payload: Optional[Dict[str, Any]] = None
    old_path: Optional[str] = None
    # --- Имя изменённого артефакта (для action=artifact_upload/delete) ---
    filename: Optional[str] = None
    kind: Optional[str] = None


class SyncChangesResponse(BaseModel):
    server_revision: int
    has_more: bool
    changes: List[SyncChange]


class SyncSnapshotResponse(BaseModel):
    server_revision: int
    total: int
    records: List[RecordFull]


class HealthResponse(BaseModel):
    status: str
    revision: int
    records_count: int
    data_root: str
    fts_enabled: bool
    time: str


# --- Проверка артефактов перед загрузкой --------------------------------
class ArtifactCheckItem(BaseModel):
    """Один файл для проверки."""
    filename: str = Field(min_length=1, max_length=250)
    sha256: Optional[str] = Field(default=None, max_length=64)


class ArtifactCheckRequest(BaseModel):
    """Пакетная проверка артефактов для одной записи."""
    artifacts: List[ArtifactCheckItem] = Field(
        default_factory=list, max_length=500
    )


class ArtifactCheckResult(BaseModel):
    """Результат проверки одного файла."""
    filename: str
    sha256: str = ""
    skip: bool = False
    reason: str = ""
    size: int = 0


class ArtifactCheckResponse(BaseModel):
    id: str
    results: List[ArtifactCheckResult] = Field(default_factory=list)