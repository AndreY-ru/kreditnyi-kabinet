"""Слои 1 и 2: жёсткие стопы и балльная скоринговая карта (раздел 6 исследования).

Баллы — в шкале шансов: оценка = A + B·ln(шансы «без дефолта»), B = PDO / ln 2. Шкала задаётся якорем
(60 баллов ↔ вероятность дефолта за год 5%) и PDO (каждые 10 баллов — шансы дефолта вдвое ниже), поэтому
любую оценку можно перевести в вероятность дефолта (pd_of). Баллы факторов и база откалиброваны
логистической регрессией (tools/calibrate.py → scorecard.json, отчёт — docs/calibration.md); границы
корзин заданы экспертно и нормативами (сроки просрочки — по 590-П). Залога в баллах нет: он не делает
компанию надёжнее, а уменьшает потери банка, поэтому работает в кредитной логике (LGD, лимит, ставка).

Конфигурация версионируется (таблица scoring_models), версия сохраняется с результатом.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CALIBRATED = json.loads(Path(__file__).with_name("scorecard.json").read_text(encoding="utf-8"))
_P = CALIBRATED["points"]

DEFAULT_MODEL: dict[str, Any] = {
    "version": CALIBRATED["version"],
    "scale": CALIBRATED["scale"],
    "base": CALIBRATED["base"],
    "zones": CALIBRATED["zones"],
    # верхняя граница (не включая) -> баллы; None = всё остальное. Границы — экспертные, баллы — калибровка
    "business_age": [[u, p] for u, p in zip([12, 24, 36, 60, None], _P["business_age"])],
    "payment_discipline": {
        "points": _P["payment_discipline"],
        # 590-П, п. 3.7 (юрлица): до 5 дней — хорошее обслуживание долга, 6–30 — среднее, больше 30 — плохое;
        # больше 90 дней — дефолт. Существенна просрочка больше 50 тыс. ₽ и больше 1% долга (как порог EBA:
        # 500 евро и 1%): мелкие и технические задержки на оценку не влияют.
        "technical_days": 5, "minor_days": 30, "default_days": 90,
        "material_amount": 50_000, "material_share": 0.01, "recent_months": 12, "minor_max_count": 2,
    },
    "guarantor": _P["guarantor"],
    "revenue_trend": _P["revenue_trend"],
    "request_to_revenue": [[u, p] for u, p in zip([0.10, 0.20, 0.35, None], _P["request_to_revenue"])],
    "debt_load": [[u, p] for u, p in zip([1.0, 2.0, 3.0, 4.0, None], _P["debt_load"])],
    "debt_load_no_profit": _P["debt_load_no_profit"],
    "tax_arrears": {"small_max": 100_000, **_P["tax_arrears"]},
    "knockouts": {"active_overdue_days": 30, "active_overdue_amount": 50_000},
    "credit": {
        "max_debt_load": 3.0,
        "manual_tax_arrears_from": 100_000,
        "round_to": 100_000,
        # Надбавка к ставке = ожидаемые потери PD × LGD (на синтетике PD — из шкалы баллов)
        "lgd_unsecured": 0.40,  # Базель III, CRE32.6: необеспеченные требования к нефинансовым компаниям
        # Вид залога: дисконт к стоимости и LGD обеспеченной части. Базель III, CRE32.11: недвижимость 20%,
        # прочее имущество 25%, дисконт 40%. Оборудование и товары дисконтируем сильнее — их дольше продавать
        "collateral": {
            "REAL_ESTATE": {"name": "Недвижимость", "haircut": 0.40, "lgd": 0.20},
            "VEHICLE": {"name": "Транспорт и спецтехника", "haircut": 0.40, "lgd": 0.25},
            "EQUIPMENT": {"name": "Оборудование", "haircut": 0.50, "lgd": 0.25},
            "GOODS": {"name": "Товары в обороте", "haircut": 0.60, "lgd": 0.25},
            "DEPOSIT": {"name": "Депозит в нашем банке", "haircut": 0.0, "lgd": 0.0},
        },
        "min_dscr": 1.25,  # прибыль до % и налогов + амортизация покрывает платежи по всем кредитам с запасом 25%
        "existing_term": 24,  # платёж по текущим долгам неизвестен — считаем, что остаток гасится за 2 года
        "existing_rate": 20.0,
        "revenue_gap": 0.3,  # выручка в анкете выше отчётности ФНС больше чем на 30% — сверяет аналитик
        # рентабельность в анкете выше, чем по отчётности ФНС, больше чем на 5 п. п. — сверяет аналитик
        "profit_gap": 0.05,
    },
}

FACTOR_LABELS = {
    "business_age": "Срок бизнеса",
    "payment_discipline": "Платёжная дисциплина",
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
    "guarantor": "COLLATERAL",
    "revenue_trend": "FINANCIALS",
    "request_to_revenue": "REQUEST_SIZE",
    "debt_load": "DEBT_LOAD",
    "tax_arrears": "TAX_ARREARS",
}

DISCIPLINE_LABELS = {"clean": "без существенных просрочек", "minor_old": "небольшие, давно",
                     "minor_recent": "небольшие, недавно или часто", "serious": "больше 30 дней",
                     "default": "больше 90 дней"}


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
    existing_monthly: int | None = None  # платёж по текущим долгам в месяц (БКИ или анкета; нет — оценим)
    fns_revenue: int | None = None  # выручка за прошлый год по «Прозрачному бизнесу»
    fns_expenses: int | None = None  # расходы за прошлый год там же: выручка − расходы ≈ прибыль до налогов
    collateral_kind: str | None = None  # REAL_ESTATE, VEHICLE, EQUIPMENT, GOODS, DEPOSIT
    collateral_value: int | None = None  # рыночная стоимость залога со слов клиента

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
    pd: float = 0.0  # вероятность дефолта за год по шкале карты


def _bin(value: float, table: list[list[Any]]) -> int:
    for i, (upper, _) in enumerate(table):
        if upper is None or value < upper:
            return i
    return len(table) - 1


def _fmt_money_short(v: int) -> str:
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.1f}".replace(".", ",").replace(",0", "") + " млн ₽"
    if abs(v) >= 1000:
        return f"{v // 1000} тыс. ₽"
    return f"{v} ₽"


def pd_of(score: float, cfg: dict[str, Any] | None = None) -> float:
    """Вероятность дефолта за год для оценки: шансы «без дефолта» удваиваются каждые PDO баллов."""
    s = (cfg or DEFAULT_MODEL).get("scale") or DEFAULT_MODEL["scale"]
    odds = (1 - s["pd"]) / s["pd"] * 2 ** ((score - s["score"]) / s["pdo"])
    return 1 / (1 + odds)


def score_of(pd: float, cfg: dict[str, Any] | None = None) -> float:
    """Обратное к pd_of: сколько баллов соответствует вероятности дефолта."""
    s = (cfg or DEFAULT_MODEL).get("scale") or DEFAULT_MODEL["scale"]
    return s["score"] + s["pdo"] * math.log2(((1 - pd) / pd) / ((1 - s["pd"]) / s["pd"]))


def classify_overdues(loans: list[dict[str, Any]], cfg: dict[str, Any]) -> tuple[str, list[dict], list[dict]]:
    """Платёжная дисциплина по отчёту БКИ: (уровень, существенные просрочки, технические).

    Техническая — до 5 дней или сумма ниже порога существенности: на оценку не влияет.
    """
    pd = cfg["payment_discipline"]
    material, technical = [], []
    for loan in loans:
        for o in loan.get("overdues_24m", []):
            big = o.get("amount", 0) > pd["material_amount"] and \
                o.get("amount", 0) > pd["material_share"] * (loan.get("balance") or 0)
            (material if o["days"] > pd["technical_days"] and big else technical).append(o)
    if not material:
        return "clean", material, technical
    worst = max(o["days"] for o in material)
    if worst > pd["default_days"]:
        return "default", material, technical
    if worst > pd["minor_days"]:
        return "serious", material, technical
    recent = any(o.get("months_ago", 0) < pd["recent_months"] for o in material)
    if recent or len(material) > pd["minor_max_count"]:
        return "minor_recent", material, technical
    return "minor_old", material, technical


def levels(inp: ScoringInputs, cfg: dict[str, Any]) -> dict[str, Any]:
    """Корзина каждого фактора. Одна и та же функция — для скоринга и для калибровки (tools/calibrate.py)."""
    load, _ = compute_debt_load(inp)
    ratio = inp.amount / inp.revenue if inp.revenue and inp.revenue > 0 else None
    ta = cfg["tax_arrears"]
    return {
        "business_age": _bin(inp.business_months or 0, cfg["business_age"]),
        "payment_discipline": classify_overdues(inp.bki_loans, cfg)[0],
        "guarantor": "yes" if inp.has_guarantor else "no",
        "revenue_trend": inp.revenue_trend if inp.revenue_trend in ("GROWTH", "DECLINE") else "FLAT",
        "request_to_revenue": _bin(ratio, cfg["request_to_revenue"]) if ratio is not None
        else len(cfg["request_to_revenue"]) - 1,
        "debt_load": "no_profit" if load is None else _bin(load, cfg["debt_load"]),
        "tax_arrears": "none" if inp.tax_arrears <= 0 else ("small" if inp.tax_arrears <= ta["small_max"] else "large"),
    }


def points_of(code: str, level: Any, cfg: dict[str, Any]) -> int:
    if code in ("business_age", "request_to_revenue") or (code == "debt_load" and level != "no_profit"):
        return int(cfg[code][level][1])
    if code == "debt_load":
        return int(cfg["debt_load_no_profit"])
    if code == "payment_discipline":
        return int(cfg[code]["points"][level])
    if code == "guarantor":
        return int(cfg[code]) if level == "yes" else 0
    if code == "revenue_trend":
        return int(cfg[code].get(level, 0))
    return 0 if level == "none" else int(cfg["tax_arrears"][level])  # tax_arrears


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


def _display(code: str, inp: ScoringInputs, cfg: dict[str, Any], level: Any) -> str:
    if code == "business_age":
        months = inp.business_months or 0
        years = months // 12
        return f"{years} лет" if years >= 5 else (f"{years} г." if years >= 1 else f"{months} мес.")
    if code == "payment_discipline":
        _, material, technical = classify_overdues(inp.bki_loans, cfg)
        text = "без существенных просрочек" if not material else \
            f"{len(material)} просроч., макс. {max(o['days'] for o in material)} дн."
        return text + (f"; мелких не учитываем: {len(technical)}" if technical else "")
    if code == "guarantor":
        return "собственник"
    if code == "revenue_trend":
        return {"GROWTH": "рост", "FLAT": "без изменений", "DECLINE": "снижение"}.get(inp.revenue_trend or "",
                                                                                     "нет данных")
    if code == "request_to_revenue":
        return f"{round(inp.amount / inp.revenue * 100)}%" if inp.revenue and inp.revenue > 0 else "нет выручки"
    if code == "debt_load":
        load, _ = compute_debt_load(inp)
        return "прибыль ≤ 0" if load is None else f"{load:.1f}×".replace(".", ",")
    return _fmt_money_short(inp.tax_arrears)


def score(inp: ScoringInputs, cfg: dict[str, Any] | None = None) -> ScoreOutcome:
    cfg = cfg or DEFAULT_MODEL
    factors: list[dict[str, Any]] = []
    for code, level in levels(inp, cfg).items():
        if (code == "guarantor" and level == "no") or (code == "tax_arrears" and level == "none"):
            continue  # показываем, только если есть
        factors.append({"code": code, "label": FACTOR_LABELS[code], "value": _display(code, inp, cfg, level),
                        "points": points_of(code, level, cfg), "reason": FACTOR_REASON[code]})

    total = max(0, min(100, cfg["base"] + sum(f["points"] for f in factors)))
    zones = cfg["zones"]
    zone = "LOW" if total < zones["decline_below"] else ("HIGH" if total >= zones["auto_from"] else "GRAY")
    load, ebitda = compute_debt_load(inp)

    factors.sort(key=lambda f: -f["points"])
    return ScoreOutcome(
        model_version=cfg["version"], base=cfg["base"], score=total, zone=zone, factors=factors,
        knockouts=check_knockouts(inp, cfg), debt_load=load, ebitda=ebitda, pd=round(pd_of(total, cfg), 4),
    )
