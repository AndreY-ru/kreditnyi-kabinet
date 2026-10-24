"""Проверка шансов до подачи: та же скоринговая карта и кредитная логика, что в конвейере.

Отличия от настоящей оценки: в БКИ не ходим (согласия ещё нет) — о просрочках спрашиваем клиента;
финансы — со слов клиента или оценка по «Прозрачному бизнесу»; в БД ничего не пишется.
Клиенту показываем категории и уровень шансов, без баллов и порогов (раздел 6.4 исследования).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy.orm import Session

from app.models import Company
from app.services import integrations
from app.services.applications import last_complete_application
from app.services.catalog import match_products
from app.services.credit_logic import decide, max_affordable
from app.services.explain import REASONS
from app.services.pipeline import active_model
from app.services.scoring import ScoringInputs, score


@dataclass
class PrecheckParams:
    goal: str
    amount: int
    term: int
    has_collateral: bool
    has_guarantor: bool
    revenue: int | None
    profit: int | None  # прибыль до процентов и налогов (с амортизацией), примерно
    debt: int  # остаток по действующим кредитам и лизингу
    trend: str  # GROWTH, FLAT, DECLINE
    overdues: str = "none"  # со слов клиента: none, minor (были небольшие), current (есть сейчас)


OVERDUES = {"none": "Не было", "minor": "Были небольшие, уже погашены", "current": "Есть сейчас"}


def _declared_history(overdues: str, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Ответ клиента о просрочках -> записи в формате отчёта БКИ, чтобы считать той же скоринговой картой.

    Границы берём из активной версии модели, а не хардкодим: поменяли пороги — проверка шансов следует за ними.
    """
    pd, ko = cfg["payment_discipline"], cfg["knockouts"]
    if overdues == "minor":
        return [{"creditor": "со слов клиента", "overdues_24m": [
            {"days": pd["minor_max_days"], "amount": 0, "months_ago": pd["recent_months"]}]}]
    if overdues == "current":
        days, amount = ko["active_overdue_days"] + 1, ko["active_overdue_amount"] + 1
        return [{"creditor": "со слов клиента", "current_overdue_days": days, "current_overdue_amount": amount,
                 "overdues_24m": [{"days": days, "amount": amount, "months_ago": 0}]}]
    return []


def defaults(db: Session, company: Company) -> dict[str, Any]:
    """Подставляем то, что уже знаем: прошлая заявка с заполненными финансами, иначе «Прозрачный бизнес»."""
    prev = last_complete_application(db, company.id)
    f = prev.financials if prev else None
    if prev and f:
        return {"revenue": f.revenue, "profit": f.ebit + (f.amortization or 0),
                "debt": sum(l.balance for l in prev.liabilities), "trend": f.revenue_trend or "FLAT",
                "source": f"из заявки {prev.number}"}
    t = integrations.fns_transparent(company.inn)
    r = t.response if t.ok and t.response else {}
    revenue, expenses = r.get("revenue_last_year") or None, r.get("expenses_last_year")
    profit = revenue - expenses if revenue and expenses is not None else None
    return {"revenue": revenue, "profit": profit, "debt": 0, "trend": "FLAT",
            "source": "оценка по «Прозрачному бизнесу» (смоделировано)" if revenue else None}


# фактор скоринга -> (заголовок, текст при «хорошо», «заметно», «плохо»)
_FACTOR_TEXT = {
    "business_age": ("Срок работы бизнеса", "Компания работает достаточно долго", "Компания довольно молодая",
                     "Компания работает слишком недолго"),
    "debt_load": ("Долговая нагрузка", "С новой суммой нагрузка комфортная",
                  "Платежи займут заметную часть прибыли", "Нагрузка высокая для такой прибыли"),
    "request_to_revenue": ("Сумма и выручка", "Сумма соразмерна выручке", "Сумма заметная относительно выручки",
                           "Сумма велика относительно выручки"),
    "revenue_trend": ("Динамика выручки", "Выручка растёт", "Выручка стабильна", "Выручка снижается"),
    "tax_arrears": ("Налоговая задолженность", "Не найдена", "Есть небольшая недоимка",
                    "Есть недоимка — погасите до подачи"),
}


def _tone(points: int) -> str:
    return "ok" if points >= 0 else ("warn" if points > -10 else "bad")


def _factor_items(factors: list[dict[str, Any]], arrears_known: bool, company: Company,
                  overdues: str) -> list[dict[str, str]]:
    by = {f["code"]: f for f in factors}
    items = []
    st_ok = company.status == "ACTIVE"
    items.append({"title": "Статус компании", "tone": "ok" if st_ok else "bad",
                  "text": "Действует по данным ЕГРЮЛ" if st_ok else "В реестре отмечена ликвидация или реорганизация"})
    for code in ("business_age", "debt_load", "request_to_revenue", "revenue_trend"):
        f = by.get(code)
        if f is None:
            continue
        title, good, mid, bad = _FACTOR_TEXT[code]
        if code == "revenue_trend":
            tone, text = {"рост": ("ok", good), "снижение": ("bad", bad)}.get(f["value"], ("ok", mid))
        else:
            tone = _tone(f["points"])
            text = {"ok": good, "warn": mid, "bad": bad}[tone]
        items.append({"title": title, "tone": tone, "text": text})
    if not arrears_known:
        items.append({"title": "Налоговая задолженность", "tone": "na", "text": "Сервис ФНС не ответил — проверим после подачи"})
    else:
        f = by.get("tax_arrears")
        title, good, mid, bad = _FACTOR_TEXT["tax_arrears"]
        tone = "ok" if f is None else _tone(f["points"])
        items.append({"title": title, "tone": tone, "text": {"ok": good, "warn": mid, "bad": bad}[tone]})
    tone, text = {"none": ("ok", "Просрочек не было — с ваших слов"),
                  "minor": ("warn", "Были небольшие просрочки — с ваших слов"),
                  "current": ("bad", "Есть текущая просрочка — погасите её до подачи")}[overdues]
    items.append({"title": "Кредитная история", "tone": tone,
                  "text": text + ". Банк проверит в бюро только после вашего согласия"})
    return items


_VERDICT = {
    "auto": ("Скорее всего, одобрим автоматически", "green"),
    "review": ("Рассмотрит специалист", "blue"),
    "low": ("Вероятен отказ", "red"),
    "unfit": ("Не подходит", "neutral"),
}


def run(db: Session, company: Company, p: PrecheckParams) -> dict[str, Any]:
    if not p.amount or p.amount <= 0 or not p.revenue or p.profit is None:
        return {"ready": False}
    t = integrations.fns_transparent(company.inn)
    arrears_known = bool(t.ok and t.response is not None)
    arrears = int((t.response or {}).get("tax_arrears") or 0) if arrears_known else 0
    cfg = active_model(db)
    base = ScoringInputs(
        amount=p.amount, term_months=p.term, business_months=company.business_months(),
        company_status=company.status, min_business_months=0, revenue=p.revenue, ebit=p.profit, amortization=0,
        revenue_trend=p.trend, existing_debt=p.debt, tax_arrears=arrears, has_collateral=p.has_collateral,
        has_guarantor=p.has_guarantor, bki_loans=_declared_history(p.overdues, cfg),
    )
    matches = match_products(db, goal=p.goal, amount=p.amount, term=p.term, has_collateral=p.has_collateral,
                             has_guarantor=p.has_guarantor, company=company, limit=20)
    products, overall_out, best_cap = [], None, None
    for m in matches:
        inp = replace(base, min_business_months=m.product.min_business_months)
        out = score(inp, cfg)
        overall_out = overall_out or out
        if not m.eligible:
            products.append({"product": m.product, "verdict": "unfit", "notes": m.warnings, "match": m})
            continue
        res = decide(m.product, inp, out, cfg)
        verdict = {"APPROVED_AUTO": "auto", "MANUAL_REVIEW": "review", "DECLINED": "low"}[res.status]
        notes = [REASONS[c]["title"] for c in res.reason_codes if c in REASONS]
        if verdict == "review" and not notes:
            notes = ["Сумма или продукт требуют решения специалиста — обычно это 1–3 дня"]
        cap = max_affordable(inp, out, m.product, cfg)  # подсказка нужнее всего как раз при низких шансах
        best_cap = cap if best_cap is None else max(best_cap, cap)
        products.append({"product": m.product, "verdict": verdict, "notes": notes, "match": m})
    products.sort(key=lambda x: ["auto", "review", "low", "unfit"].index(x["verdict"]))

    out = overall_out
    assert out is not None
    if company.status != "ACTIVE" or not any(x["verdict"] in ("auto", "review") for x in products):
        level = "low"
    elif any(x["verdict"] == "auto" for x in products) or out.zone == "HIGH":
        level = "high"
    else:
        level = "medium" if out.zone == "GRAY" else "low"

    tips: list[str] = []
    negative = sorted((f for f in out.factors if f["points"] < 0), key=lambda f: f["points"])
    for code in dict.fromkeys(f["reason"] for f in negative):
        if code in REASONS and REASONS[code]["action"] not in tips:
            tips.append(REASONS[code]["action"])
    if best_cap is not None and 0 < best_cap < p.amount:
        tips.insert(0, f"Комфортная сумма для ваших показателей — до {best_cap:,} ₽".replace(",", " "))
    return {
        "ready": True, "level": level,
        "items": _factor_items(out.factors, arrears_known, company, p.overdues if p.overdues in OVERDUES else "none"),
        "products": products, "verdicts": _VERDICT, "tips": tips[:4], "cap": best_cap,
    }
