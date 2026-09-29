"""Конфигурация сервиса.

Читается из переменных окружения (12-factor). Все значения
имеют безопасные дефолты, чтобы сервис поднимался «из коробки»
при локальной отладке.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- auth ---
    api_key: str = Field(default="dev-key", alias="SCREC_API_KEY")

    # --- storage ---
    data_root: Path = Field(default=Path("./data"), alias="SCREC_DATA_ROOT")
    log_root: Path = Field(default=Path("./logs"), alias="SCREC_LOG_ROOT")

    # --- server ---
    host: str = Field(default="0.0.0.0", alias="SCREC_HOST")
    port: int = Field(default=8000, alias="SCREC_PORT")
    log_level: str = Field(default="INFO", alias="SCREC_LOG_LEVEL")
    access_log: bool = Field(default=True, alias="SCREC_ACCESS_LOG")
    openapi_enabled: bool = Field(
        default=True, alias="SCREC_OPENAPI_ENABLED"
    )

    # --- limits ---
    max_artifact_mb: int = Field(
        default=50, alias="SCREC_MAX_ARTIFACT_MB"
    )
    max_payload_kb: int = Field(
        default=512, alias="SCREC_MAX_PAYLOAD_KB"
    )
    sync_page_size: int = Field(
        default=200, alias="SCREC_SYNC_PAGE_SIZE"
    )
    fts_enabled: bool = Field(default=True, alias="SCREC_FTS_ENABLED")

    @property
    def records_root(self) -> Path:
        return self.data_root / "records"

    @property
    def max_artifact_bytes(self) -> int:
        return self.max_artifact_mb * 1024 * 1024

    @property
    def max_payload_bytes(self) -> int:
        return self.max_payload_kb * 1024


settings = Settings()


def ensure_dirs() -> None:
    """Создаёт рабочие каталоги, если их нет."""
    settings.data_root.mkdir(parents=True, exist_ok=True)
    settings.records_root.mkdir(parents=True, exist_ok=True)
    settings.log_root.mkdir(parents=True, exist_ok=True)
    (settings.log_root / "access").mkdir(parents=True, exist_ok=True)