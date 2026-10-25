"""Мок ФНС: ЕГРЮЛ/ЕГРИП и «Прозрачный бизнес». СМОДЕЛИРОВАНО, реальные сервисы ФНС не вызываются.

Контракт упрощённый: GET /egrul/{inn}, GET /transparent/{inn}.
Сбои: для ИНН из FNS_ALWAYS_DOWN сервис «Прозрачный бизнес» всегда отвечает 503 (сценарий T8);
FNS_FAIL_INNS — дополнительные ИНН со сбоем обоих методов; FNS_FAIL_RATE (0..1) — доля случайных 503.
"""
from __future__ import annotations

import os
import random
from datetime import datetime, timezone

from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from mocks.data import (
    COMPANIES,
    FNS_ALWAYS_DOWN,
    get_company,
    inn_is_valid,
    known_companies,
    new_inn,
    overrides,
    save_override,
)

app = FastAPI(title="FNS mock (смоделировано)", version="1.2.1")
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


# --- Тестовые данные (только для демо): правка сведений о компании --------------------------------------

class CompanyPatch(BaseModel):
    status: Literal["ACTIVE", "LIQUIDATING", "REORGANIZING"] | None = None
    registered_at: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    tax_arrears: int | None = Field(default=None, ge=0, le=10**12)
    revenue_last_year: int | None = Field(default=None, ge=0, le=10**15)
    expenses_last_year: int | None = Field(default=None, ge=0, le=10**15)
    employees: int | None = Field(default=None, ge=0, le=1_000_000)


def _admin_view(inn: str) -> dict:
    company = get_company(inn)
    if company is None:
        raise HTTPException(status_code=404, detail="Некорректный ИНН")
    e, t = company["egrul"], company["transparent"]
    return {"inn": inn, "name": e.get("name"), "status": e.get("status"), "registered_at": e.get("registered_at"),
            "tax_arrears": t.get("tax_arrears"), "revenue_last_year": t.get("revenue_last_year"),
            "expenses_last_year": t.get("expenses_last_year"), "employees": t.get("employees"),
            "overridden": inn in overrides("fns")}


@app.get("/admin/companies/{inn}", tags=["тестовые данные"])
def admin_company(inn: str) -> dict:
    """Ключевые сведения ЕГРЮЛ и «Прозрачного бизнеса» — с учётом правок."""
    return _admin_view(inn)


@app.put("/admin/companies/{inn}", tags=["тестовые данные"])
def admin_company_save(inn: str, body: CompanyPatch) -> dict:
    """Поменять сведения о компании (СМОДЕЛИРОВАНО: в настоящей ФНС так нельзя)."""
    _admin_view(inn)
    d = body.model_dump(exclude_none=True)
    egrul = {k: d[k] for k in ("status", "registered_at") if k in d}
    transparent = {k: d[k] for k in ("tax_arrears", "revenue_last_year", "expenses_last_year", "employees") if k in d}
    save_override("fns", inn, {"egrul": egrul, "transparent": transparent})
    return _admin_view(inn)


@app.delete("/admin/companies/{inn}", tags=["тестовые данные"])
def admin_company_reset(inn: str) -> dict:
    """Вернуть исходные сведения."""
    _admin_view(inn)
    save_override("fns", inn, None)
    return _admin_view(inn)


class CompanyNew(BaseModel):
    """Новая синтетическая компания для демо. ИНН не указан — сгенерируем корректный."""
    inn: str | None = Field(default=None, pattern=r"^\d{10}(\d{2})?$")
    kind: Literal["OOO", "IP"] = "OOO"
    name: str = Field(min_length=2, max_length=200)
    director: str = Field(default="", max_length=120)
    registered_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    okved: str = Field(default="46.90", pattern=r"^\d{2}(\.\d{1,2}){0,2}$")
    okved_name: str = Field(default="Торговля оптовая неспециализированная", max_length=200)
    address: str = Field(default="г. Москва (синтетический адрес)", max_length=200)
    status: Literal["ACTIVE", "LIQUIDATING", "REORGANIZING"] = "ACTIVE"
    employees: int = Field(default=10, ge=0, le=1_000_000)
    tax_arrears: int = Field(default=0, ge=0, le=10**12)
    revenue_last_year: int = Field(default=30_000_000, ge=0, le=10**15)
    expenses_last_year: int = Field(default=27_000_000, ge=0, le=10**15)


@app.get("/admin/companies", tags=["тестовые данные"])
def admin_companies() -> list[dict]:
    """Компании справочника и добавленные для демо, с отметкой о правках."""
    edited = overrides("fns")
    return [{**row, "overridden": row["inn"] in edited} for row in known_companies()]


@app.post("/admin/companies", status_code=201, tags=["тестовые данные"])
def admin_company_create(body: CompanyNew) -> dict:
    """Добавить синтетическую компанию: появится в ЕГРЮЛ, «Прозрачном бизнесе» и БКИ (без кредитов)."""
    kind = body.kind if body.inn is None else ("OOO" if len(body.inn) == 10 else "IP")
    inn = body.inn or new_inn(kind)
    while body.inn is None and (inn in COMPANIES or inn in overrides("companies")):
        inn = new_inn(kind)
    if not inn_is_valid(inn):
        raise HTTPException(status_code=422, detail="Неверная контрольная сумма ИНН")
    if inn in COMPANIES or inn in overrides("companies"):
        raise HTTPException(status_code=409, detail="Компания с таким ИНН уже есть")
    ogrn = new_inn("IP") + new_inn("IP")
    save_override("companies", inn, {
        "egrul": {"kind": kind, "ogrn": ogrn[:15 if kind == "IP" else 13], "name": body.name,
                  "registered_at": body.registered_at, "status": body.status, "address": body.address,
                  "okved": body.okved, "okved_name": body.okved_name, "director": body.director or None,
                  "founders": [] if kind == "IP" else [{"name": body.director or "Учредитель", "share": 100}]},
        "transparent": {"employees": body.employees, "tax_arrears": body.tax_arrears,
                        "taxes_paid_last_year": body.revenue_last_year // 25,
                        "revenue_last_year": body.revenue_last_year, "expenses_last_year": body.expenses_last_year},
        "bki": [],
    })
    return _admin_view(inn)


@app.delete("/admin/custom/{inn}", tags=["тестовые данные"])
def admin_company_remove(inn: str) -> dict:
    """Удалить добавленную для демо компанию вместе с её правками. Компании справочника не удаляются."""
    if inn not in overrides("companies"):
        raise HTTPException(status_code=404, detail="Такой добавленной компании нет")
    save_override("companies", inn, None)
    save_override("fns", inn, None)
    save_override("bki", inn, None)
    return {"inn": inn, "removed": True}
