"""Слои 1 и 2: жёсткие стопы и балльная скоринговая карта (раздел 6 исследования).

Все баллы и пороги — настраиваемые параметры на синтетических данных, НЕ откалиброваны.
Конфигурация версионируется (таблица scoring_models), версия сохраняется с результатом.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_MODEL: dict[str, Any] = {
    "version": "v0.3",
    "base": 55,
    "zones": {"decline_below": 45, "auto_from": 75},
    # верхняя граница (мес., не включая) -> баллы; None = всё остальное
    "business_age": [[12, -5], [24, 0], [36, 4], [60, 8], [None, 12]],
    "payment_discipline": {"clean": 10, "minor": 0, "major": -12, "minor_max_days": 30, "minor_max_count": 2,
                            "recent_months": 12},
    "collateral": 8,
    "guarantor": 6,
    "revenue_trend": {"GROWTH": 5, "FLAT": 0, "DECLINE": -8},
    "request_to_revenue": [[0.10, 0], [0.20, -7], [0.35, -12], [None, -20]],
    "debt_load": [[1.0, 5], [2.0, -6], [3.0, -18], [4.0, -25], [None, -35]],
    "debt_load_no_profit": -35,
    "tax_arrears": {"small_max": 100_000, "small": -5, "large": -12},
    "knockouts": {"active_overdue_days": 30, "active_overdue_amount": 50_000},
    "credit": {
        "max_debt_load": 3.0,
        "manual_tax_arrears_from": 100_000,
        "round_to": 100_000,
        "grade_surcharge": [[85, 0.0], [75, 1.5], [60, 3.0], [0, 4.5]],
    },
}

FACTOR_LABELS = {
    "business_age": "Срок бизнеса",
    "payment_discipline": "Платёжная дисциплина",
    "collateral": "Залог",
    "guarantor": "Поручительство",
    "revenue_trend": "Динамика выручки",
    "request_to_revenue": "Запрос к выручке",
    "debt_load": "Долговая нагрузка",
    "tax_arrears": "Налоговая недоимка",
}

# фактор -> категория причины для клиента
FACTOR_REASON = {
    "business_age": "BUSINESS_AGE",
    "payment_discipline": "PAYMENT_HISTORY",
    "collateral": "COLLATERAL",
    "guarantor": "COLLATERAL",
    "revenue_trend": "FINANCIALS",
    "request_to_revenue": "REQUEST_SIZE",
    "debt_load": "DEBT_LOAD",
    "tax_arrears": "TAX_ARREARS",
}


@dataclass
class ScoringInputs:
    amount: int
    term_months: int
    business_months: int | None
    company_status: str
    min_business_months: int
    revenue: int | None
    ebit: int | None
    amortization: int | None
    revenue_trend: str | None
    existing_debt: int  # из БКИ (если доступно) или из анкеты — берём большее
    tax_arrears: int
    has_collateral: bool
    has_guarantor: bool
    bki_loans: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class ScoreOutcome:
    model_version: str
    base: int
    score: int
    zone: str  # HIGH, GRAY, LOW
    factors: list[dict[str, Any]]
    knockouts: list[dict[str, Any]]
    debt_load: float | None
    ebitda: int | None


def _bucket(value: float, table: list[list[Any]]) -> int:
    for upper, points in table:
        if upper is None or value < upper:
            return int(points)
    return int(table[-1][1])


def _fmt_money_short(v: int) -> str:
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.1f}".replace(".", ",").replace(",0", "") + " млн ₽"
    if abs(v) >= 1000:
        return f"{v // 1000} тыс. ₽"
    return f"{v} ₽"


def check_knockouts(inp: ScoringInputs, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    ko: list[dict[str, Any]] = []
    if inp.company_status != "ACTIVE":
        ko.append({"code": "COMPANY_STATUS", "label": "Компания в процессе ликвидации или реорганизации",
                   "reason": "COMPANY_STATUS"})
    if inp.business_months is not None and inp.business_months < inp.min_business_months:
        ko.append({"code": "BUSINESS_AGE", "label": f"Срок бизнеса {inp.business_months} мес. меньше минимума продукта "
                   f"({inp.min_business_months} мес.)", "reason": "BUSINESS_AGE", "product_specific": True})
    k = cfg["knockouts"]
    for loan in inp.bki_loans:
        if loan.get("current_overdue_days", 0) > k["active_overdue_days"] and \
                loan.get("current_overdue_amount", 0) > k["active_overdue_amount"]:
            ko.append({"code": "ACTIVE_OVERDUE", "label": f"Действующая просрочка {loan['current_overdue_days']} дн. "
                       f"на {_fmt_money_short(loan['current_overdue_amount'])} ({loan.get('creditor', '')})",
                       "reason": "PAYMENT_HISTORY"})
            break
    return ko


def compute_debt_load(inp: ScoringInputs, extra: int | None = None) -> tuple[float | None, int | None]:
    if inp.ebit is None:
        return None, None
    ebitda = inp.ebit + (inp.amortization or 0)
    if ebitda <= 0:
        return None, ebitda
    total = inp.existing_debt + (inp.amount if extra is None else extra)
    return total / ebitda, ebitda


def score(inp: ScoringInputs, cfg: dict[str, Any] | None = None) -> ScoreOutcome:
    cfg = cfg or DEFAULT_MODEL
    factors: list[dict[str, Any]] = []

    def add(code: str, display: str, points: int) -> None:
        factors.append({"code": code, "label": FACTOR_LABELS[code], "value": display, "points": points,
                        "reason": FACTOR_REASON[code]})

    # Срок бизнеса
    months = inp.business_months or 0
    years = months // 12
    age_disp = f"{years} лет" if years >= 5 else (f"{years} г." if years >= 1 else f"{months} мес.")
    add("business_age", age_disp, _bucket(months, cfg["business_age"]))

    # Платёжная дисциплина
    pd = cfg["payment_discipline"]
    overdues = [o for loan in inp.bki_loans for o in loan.get("overdues_24m", [])]
    if not overdues:
        add("payment_discipline", "0 просрочек", pd["clean"])
    else:
        worst = max(o["days"] for o in overdues)
        recent = [o for o in overdues if o.get("months_ago", 0) < pd["recent_months"]]
        minor = worst <= pd["minor_max_days"] and len(overdues) <= pd["minor_max_count"] and not recent
        add("payment_discipline", f"{len(overdues)} просроч., макс. {worst} дн.", pd["minor"] if minor else pd["major"])

    # Обеспечение
    if inp.has_collateral:
        add("collateral", "есть", cfg["collateral"])
    if inp.has_guarantor:
        add("guarantor", "собственник", cfg["guarantor"])

    # Динамика выручки
    trend_disp = {"GROWTH": "рост", "FLAT": "без изменений", "DECLINE": "снижение"}.get(inp.revenue_trend or "", "нет данных")
    add("revenue_trend", trend_disp, cfg["revenue_trend"].get(inp.revenue_trend or "", 0))

    # Запрос к выручке
    if inp.revenue and inp.revenue > 0:
        ratio = inp.amount / inp.revenue
        add("request_to_revenue", f"{round(ratio * 100)}%", _bucket(ratio, cfg["request_to_revenue"]))
    else:
        add("request_to_revenue", "нет выручки", int(cfg["request_to_revenue"][-1][1]))

    # Долговая нагрузка: (долг + запрос) / (EBIT + амортизация)
    load, ebitda = compute_debt_load(inp)
    if load is None:
        add("debt_load", "прибыль ≤ 0", cfg["debt_load_no_profit"])
    else:
        add("debt_load", f"{load:.1f}×".replace(".", ","), _bucket(load, cfg["debt_load"]))

    # Налоговая недоимка
    ta = cfg["tax_arrears"]
    if inp.tax_arrears > 0:
        pts = ta["small"] if inp.tax_arrears <= ta["small_max"] else ta["large"]
        add("tax_arrears", _fmt_money_short(inp.tax_arrears), pts)

    total = cfg["base"] + sum(f["points"] for f in factors)
    total = max(0, min(100, total))
    zones = cfg["zones"]
    zone = "LOW" if total < zones["decline_below"] else ("HIGH" if total >= zones["auto_from"] else "GRAY")

    factors.sort(key=lambda f: -f["points"])
    return ScoreOutcome(
        model_version=cfg["version"], base=cfg["base"], score=total, zone=zone, factors=factors,
        knockouts=check_knockouts(inp, cfg), debt_load=load, ebitda=ebitda,
    )
