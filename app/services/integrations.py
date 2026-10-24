"""Клиенты к мокам ФНС и БКИ. Контракт упрощённый, чтобы мок можно было заменить реальной интеграцией."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings


@dataclass
class CallResult:
    source: str
    endpoint: str
    request: dict[str, Any]
    response: dict[str, Any] | None
    http_status: int | None
    ok: bool
    duration_ms: int


# Фабрика HTTP-клиента: в тестах подменяется на TestClient(mock_app)
HttpFactory = Callable[[str], httpx.Client]


def _default_factory(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=settings.http_timeout)


http_factory: dict[str, HttpFactory] = {}


def _client_for(source: str, base_url: str) -> httpx.Client:
    factory = http_factory.get(source, _default_factory)
    return factory(base_url)


def _call(source: str, base_url: str, path: str, params: dict[str, Any] | None = None) -> CallResult:
    started = time.monotonic()
    try:
        with _client_for(source, base_url) as client:
            resp = client.get(path, params=params)
    except httpx.HTTPError as exc:
        return CallResult(source, path, params or {}, {"error": str(exc)}, None, False,
                          int((time.monotonic() - started) * 1000))
    duration = int((time.monotonic() - started) * 1000)
    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text[:500]}
    return CallResult(source, path, params or {}, body, resp.status_code, resp.is_success, duration)


def fns_egrul(inn: str) -> CallResult:
    return _call("FNS_MOCK", settings.fns_url, f"/egrul/{inn}")


def fns_transparent(inn: str) -> CallResult:
    return _call("FNS_MOCK", settings.fns_url, f"/transparent/{inn}")


def bki_report(inn: str, consent_id: int, purpose: str) -> CallResult:
    return _call("BKI_MOCK", settings.bki_url, f"/report/{inn}",
                 {"consent_id": str(consent_id), "purpose": purpose})
