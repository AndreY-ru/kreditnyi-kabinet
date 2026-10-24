"""Настройки приложения. Всё берётся из переменных окружения (секреты не хранятся в коде)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL", "postgresql+psycopg://kabinet:kabinet@localhost:5432/kabinet"
        )
    )
    secret_key: str = field(default_factory=lambda: os.getenv("SECRET_KEY", "dev-only-change-me"))
    secure_cookies: bool = field(default_factory=lambda: _bool(os.getenv("SECURE_COOKIES"), False))

    # Моки внешних сервисов (отдельные контейнеры)
    fns_url: str = field(default_factory=lambda: os.getenv("FNS_MOCK_URL", "http://localhost:8101"))
    bki_url: str = field(default_factory=lambda: os.getenv("BKI_MOCK_URL", "http://localhost:8102"))
    http_timeout: float = field(default_factory=lambda: float(os.getenv("HTTP_TIMEOUT", "3")))

    # Конвейер
    enrich_max_attempts: int = field(default_factory=lambda: int(os.getenv("ENRICH_MAX_ATTEMPTS", "3")))
    enrich_retry_delay: float = field(default_factory=lambda: float(os.getenv("ENRICH_RETRY_DELAY", "1.0")))
    pipeline_step_delay: float = field(default_factory=lambda: float(os.getenv("PIPELINE_STEP_DELAY", "0.8")))
    worker_poll_interval: float = field(default_factory=lambda: float(os.getenv("WORKER_POLL_INTERVAL", "1.0")))
    run_worker_in_app: bool = field(default_factory=lambda: _bool(os.getenv("RUN_WORKER_IN_APP"), False))

    # Сроки (в днях)
    offer_valid_days: int = field(default_factory=lambda: int(os.getenv("OFFER_VALID_DAYS", "14")))
    info_request_days: int = field(default_factory=lambda: int(os.getenv("INFO_REQUEST_DAYS", "7")))
    review_sla_hours: int = field(default_factory=lambda: int(os.getenv("REVIEW_SLA_HOURS", "8")))
    bki_consent_months: int = 6

    # Документы
    upload_dir: str = field(default_factory=lambda: os.getenv("UPLOAD_DIR", "./data/uploads"))
    max_upload_mb: int = field(default_factory=lambda: int(os.getenv("MAX_UPLOAD_MB", "20")))

    # LLM с открытыми весами (необязательно). Пусто = правила и шаблоны без модели.
    llm_url: str | None = field(default_factory=lambda: os.getenv("LLM_URL") or None)
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "qwen3:4b-instruct-2507-q4_K_M"))
    llm_timeout: float = field(default_factory=lambda: float(os.getenv("LLM_TIMEOUT", "30")))

    display_tz: str = field(default_factory=lambda: os.getenv("DISPLAY_TZ", "Europe/Moscow"))


settings = Settings()
