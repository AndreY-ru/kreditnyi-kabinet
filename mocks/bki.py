"""Мок бюро кредитных историй. СМОДЕЛИРОВАНО, реальное БКИ не вызывается.

Без идентификатора согласия (consent_id) и цели запроса отчёт не выдаётся (218-ФЗ, ст. 6).
"""
from __future__ import annotations

import os
import random
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query

from mocks.data import get_company

app = FastAPI(title="BKI mock (смоделировано)", version="1.1.0")
FAIL_RATE = float(os.getenv("BKI_FAIL_RATE", "0"))


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/report/{inn}")
def report(inn: str, consent_id: str | None = Query(None), purpose: str | None = Query(None)) -> dict:
    if not consent_id or not purpose:
        raise HTTPException(status_code=403, detail="Нет согласия субъекта на запрос кредитного отчёта")
    if FAIL_RATE and random.random() < FAIL_RATE:
        raise HTTPException(status_code=503, detail="БКИ временно недоступно (смоделировано)")
    company = get_company(inn)
    if company is None:
        raise HTTPException(status_code=404, detail="Субъект не найден")
    loans = company["bki"]
    return {
        "inn": inn,
        "consent_id": consent_id,
        "loans": loans,
        "total_balance": sum(x["balance"] for x in loans),
        "total_monthly": sum(x["monthly_payment"] for x in loans),
        "inquiries_30d": len(loans) % 3,
        "_meta": {"source": "BKI_MOCK", "simulated": True, "generated_at": datetime.now(timezone.utc).isoformat()},
    }
