"""Мок бюро кредитных историй. СМОДЕЛИРОВАНО, реальное БКИ не вызывается.

Без идентификатора согласия (consent_id) и цели запроса отчёт не выдаётся (218-ФЗ, ст. 6).
"""
from __future__ import annotations

import os
import random
from datetime import datetime, timezone

from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from mocks.data import get_company, overrides, save_override

app = FastAPI(title="BKI mock (смоделировано)", version="1.2.1")
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


# --- Тестовые данные (только для демо): правка кредитной истории компании -------------------------------

class Overdue(BaseModel):
    days: int = Field(ge=1, le=3650, description="Сколько дней длилась просрочка")
    amount: int = Field(ge=0, le=10**12, description="Сумма просрочки, ₽")
    months_ago: int = Field(ge=0, le=23, description="Сколько месяцев назад (история — за 24 месяца)")


class Loan(BaseModel):
    creditor: str = Field(min_length=1, max_length=120)
    kind: Literal["кредит", "лизинг", "овердрафт", "гарантия"] = "кредит"
    balance: int = Field(ge=0, le=10**13)
    monthly_payment: int = Field(default=0, ge=0, le=10**12)
    maturity: str | None = Field(default=None, pattern=r"^(0[1-9]|1[0-2])\.\d{4}$")
    current_overdue_days: int = Field(default=0, ge=0, le=3650, description="Действующая просрочка, дней")
    current_overdue_amount: int = Field(default=0, ge=0, le=10**12)
    overdues_24m: list[Overdue] = Field(default_factory=list, max_length=50)


class ReportIn(BaseModel):
    loans: list[Loan] = Field(max_length=30)


def _admin_view(inn: str) -> dict:
    company = get_company(inn)
    if company is None:
        raise HTTPException(status_code=404, detail="Некорректный ИНН")
    return {"inn": inn, "loans": company["bki"], "overridden": inn in overrides("bki")}


@app.get("/admin/reports/{inn}", tags=["тестовые данные"])
def admin_report(inn: str) -> dict:
    """Кредитная история компании как её отдаст /report — с учётом правок."""
    return _admin_view(inn)


@app.put("/admin/reports/{inn}", tags=["тестовые данные"])
def admin_report_save(inn: str, body: ReportIn) -> dict:
    """Заменить кредитную историю компании (СМОДЕЛИРОВАНО: настоящее БКИ так не умеет)."""
    _admin_view(inn)
    save_override("bki", inn, [loan.model_dump() for loan in body.loans])
    return _admin_view(inn)


@app.delete("/admin/reports/{inn}", tags=["тестовые данные"])
def admin_report_reset(inn: str) -> dict:
    """Вернуть исходную кредитную историю."""
    _admin_view(inn)
    save_override("bki", inn, None)
    return _admin_view(inn)
