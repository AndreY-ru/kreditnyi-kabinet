"""Мок ФНС: ЕГРЮЛ/ЕГРИП и «Прозрачный бизнес». СМОДЕЛИРОВАНО, реальные сервисы ФНС не вызываются.

Контракт упрощённый: GET /egrul/{inn}, GET /transparent/{inn}.
Сбои: для ИНН из FNS_ALWAYS_DOWN сервис «Прозрачный бизнес» всегда отвечает 503 (сценарий T8);
FNS_FAIL_INNS — дополнительные ИНН со сбоем обоих методов; FNS_FAIL_RATE (0..1) — доля случайных 503.
"""
from __future__ import annotations

import os
import random
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException

from mocks.data import FNS_ALWAYS_DOWN, get_company

app = FastAPI(title="FNS mock (смоделировано)", version="0.1.0")
FAIL_RATE = float(os.getenv("FNS_FAIL_RATE", "0"))
EXTRA_DOWN = {x.strip() for x in os.getenv("FNS_FAIL_INNS", "").split(",") if x.strip()}


def _maybe_fail(inn: str, endpoint: str) -> None:
    always = endpoint == "transparent" and inn in FNS_ALWAYS_DOWN
    if always or inn in EXTRA_DOWN or (FAIL_RATE and random.random() < FAIL_RATE):
        raise HTTPException(status_code=503, detail="Сервис ФНС временно недоступен (смоделировано)")


def _meta() -> dict:
    return {"source": "FNS_MOCK", "simulated": True, "generated_at": datetime.now(timezone.utc).isoformat()}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/egrul/{inn}")
def egrul(inn: str) -> dict:
    _maybe_fail(inn, "egrul")
    company = get_company(inn)
    if company is None:
        raise HTTPException(status_code=404, detail="Не найдено в реестре")
    return {"inn": inn, **company["egrul"], "_meta": _meta()}


@app.get("/transparent/{inn}")
def transparent(inn: str) -> dict:
    _maybe_fail(inn, "transparent")
    company = get_company(inn)
    if company is None:
        raise HTTPException(status_code=404, detail="Не найдено")
    return {"inn": inn, **company["transparent"], "_meta": _meta()}
