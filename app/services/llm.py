"""Клиент к LLM с открытыми весами (Ollama).

Модель в проекте только помогает людям: разбирает запрос клиента, написанный своими словами
(app/services/intake.py), и предлагает аналитику черновик сообщения клиенту
(app/services/client_message.py). Кредитных решений она не принимает.

Правила для всех вызовов: строгий формат ответа (JSON-схема), короткий таймаут, на любой сбой — None,
и вызывающий код берёт детерминированный вариант. Основной сценарий от модели не зависит.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger(__name__)


_PAUSE_AFTER_FAILURE = 30.0  # сек.: не ждать таймаут на каждом запросе, если модель лежит
_down_until = 0.0


def enabled() -> bool:
    return bool(settings.llm_url)


def _chat(system: str, user: str, *, fmt: dict[str, Any] | None, max_tokens: int) -> str | None:
    global _down_until
    if not enabled() or time.monotonic() < _down_until:
        return None
    payload: dict[str, Any] = {
        "model": settings.llm_model, "stream": False, "think": False, "keep_alive": "60m",
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "options": {"temperature": 0, "num_ctx": 2048, "num_predict": max_tokens},
    }
    if fmt is not None:
        payload["format"] = fmt
    try:
        resp = httpx.post(f"{settings.llm_url.rstrip('/')}/api/chat", json=payload, timeout=settings.llm_timeout)
        resp.raise_for_status()
        return (resp.json().get("message", {}).get("content") or "").strip() or None
    except (httpx.HTTPError, ValueError) as exc:
        if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException)):
            _down_until = time.monotonic() + _PAUSE_AFTER_FAILURE
        log.warning("LLM недоступна, работаем без неё: %s", exc)
        return None


def chat_json(system: str, user: str, schema: dict[str, Any], max_tokens: int = 200) -> dict[str, Any] | None:
    raw = _chat(system, user, fmt=schema, max_tokens=max_tokens)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("LLM вернула не JSON: %.200s", raw)
        return None
    return data if isinstance(data, dict) else None


def chat_text(system: str, user: str, max_tokens: int = 300) -> str | None:
    return _chat(system, user, fmt=None, max_tokens=max_tokens)


# --- Проверка текста, который увидит клиент ------------------------------------------------

_GROUPED = re.compile(r"(?<=\d)[   ](?=\d{3}(?!\d))")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
PROMISES = ("гарантир", "точно одобр", "обязательно одобр", "одобрим")


def numbers(text: str) -> set[str]:
    """Числа в тексте; «1 000 000» и «1000000», «2,5» и «2.5» считаются одинаковыми."""
    return {n.replace(",", ".") for n in _NUMBER.findall(_GROUPED.sub("", text))}


def safe_text(candidate: str, *, source: str, forbidden: tuple[str, ...] = (), min_len: int = 20,
              max_len: int = 700) -> str | None:
    """Причина отказа от текста модели или None, если текст можно показать клиенту."""
    text = candidate.strip()
    if not min_len <= len(text) <= max_len:
        return "длина"
    letters = [ch.lower() for ch in text if ch.isalpha()]
    if not letters or sum("а" <= ch <= "я" or ch == "ё" for ch in letters) / len(letters) < 0.8:
        return "не русский текст"
    if numbers(text) - numbers(source):
        return "новые числа"
    low = text.lower().replace("ё", "е")
    for word in PROMISES + forbidden:
        if word in low:
            return f"запрещённое слово «{word}»"
    return None
