"""Слой 3: кредитная логика продукта.

Одинаковая оценка может дать разные условия для разных продуктов. На вход: оценка, продукт,
сумма, срок, стопы. На выход: авто-одобрение, отказ или ручной разбор (с подсказкой для аналитика).
Ставка = базовая ставка продукта + надбавка по грейду оценки (параметры синтетические).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.models import Product
from app.services.scoring import ScoreOutcome, ScoringInputs


@dataclass
class CreditOutcome:
    status: str  # APPROVED_AUTO, DECLINED, MANUAL_REVIEW
    reason_codes: list[str] = field(default_factory=list)
    review_reasons: list[str] = field(default_factory=list)
    offer: dict[str, Any] | None = None
    hint: dict[str, Any] | None = None
    priority: int = 0


def _mln(v: int) -> str:
    return f"{v / 1_000_000:.1f}".replace(".0", "").replace(".", ",") + " млн ₽"


def rate_for(product: Product, score: int, cfg: dict[str, Any]) -> Decimal:
    surcharge = 0.0
    for min_score, s in cfg["credit"]["grade_surcharge"]:
        if score >= min_score:
            surcharge = s
            break
    return (Decimal(str(product.base_rate)) + Decimal(str(surcharge))).quantize(Decimal("0.01"))


def annuity(amount: int, rate: Decimal, months: int) -> int:
    r = float(rate) / 100 / 12
    if months <= 0:
        return amount
    if r == 0:
        return round(amount / months)
    payment = amount * r / (1 - (1 + r) ** -months)
    return int(Decimal(payment).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def max_affordable(inp: ScoringInputs, out: ScoreOutcome, product: Product, cfg: dict[str, Any]) -> int | None:
    """Максимальная сумма, при которой долговая нагрузка не выше допустимой, с учётом лимита без залога."""
    caps: list[int] = [product.max_amount]
    if out.ebitda is not None and out.ebitda > 0:
        caps.append(int(cfg["credit"]["max_debt_load"] * out.ebitda - inp.existing_debt))
    elif out.ebitda is not None:
        caps.append(0)
    if not inp.has_collateral and product.unsecured_max:
        caps.append(product.unsecured_max)
    cap = min(caps)
    step = cfg["credit"]["round_to"]
    cap = (cap // step) * step
    return max(cap, 0)


def decide(product: Product, inp: ScoringInputs, out: ScoreOutcome, cfg: dict[str, Any]) -> CreditOutcome:
    # 1. Жёсткие стопы
    if out.knockouts:
        return CreditOutcome("DECLINED", reason_codes=sorted({k["reason"] for k in out.knockouts}))

    # 2. Низкая зона
    if out.zone == "LOW":
        negative = [f for f in out.factors if f["points"] < 0]
        negative.sort(key=lambda f: f["points"])
        codes = list(dict.fromkeys(f["reason"] for f in negative[:2])) or ["FINANCIALS"]
        return CreditOutcome("DECLINED", reason_codes=codes)

    # 3. Ручной разбор или авто-одобрение
    review: list[str] = []
    codes: list[str] = []
    priority = 0
    cap = max_affordable(inp, out, product, cfg)

    if out.zone == "GRAY":
        review.append(f"Оценка {out.score} в серой зоне")
    if product.auto_limit == 0:
        review.append("Продукт рассматривается только специалистом" + (" (господдержка)" if product.gov_support else ""))
    elif inp.amount > product.auto_limit:
        review.append(f"Сумма выше авто-лимита продукта ({product.auto_limit // 1_000_000} млн ₽)")
    if inp.tax_arrears >= cfg["credit"]["manual_tax_arrears_from"]:
        review.append(f"Налоговая недоимка {inp.tax_arrears:,} ₽".replace(",", " "))
        codes.append("TAX_ARREARS")
        priority += 2
    if cap is not None and inp.amount > cap:
        if not inp.has_collateral and product.unsecured_max and inp.amount > product.unsecured_max:
            review.append(f"Без залога доступно не больше {product.unsecured_max // 1_000_000} млн ₽")
            codes.append("COLLATERAL")
        else:
            review.append("Сумма выше допустимой долговой нагрузки")
            codes.append("DEBT_LOAD")
        priority += 1

    rate = rate_for(product, out.score, cfg)
    if not review:
        return CreditOutcome(
            "APPROVED_AUTO",
            offer={"amount": inp.amount, "term_months": inp.term_months, "rate": rate,
                   "monthly_payment": annuity(inp.amount, rate, inp.term_months)},
        )

    hint = None
    if cap is not None and 0 < cap < inp.amount and cap >= product.min_amount:
        hint = {"amount": cap, "term_months": inp.term_months, "rate": str(rate),
                "text": f"При текущей нагрузке допустимо до {_mln(cap)} на {inp.term_months} мес."}
    elif cap is not None and cap >= inp.amount:
        hint = {"amount": inp.amount, "term_months": inp.term_months, "rate": str(rate),
                "text": "Запрошенная сумма укладывается в допустимую нагрузку"}
    return CreditOutcome("MANUAL_REVIEW", reason_codes=codes, review_reasons=review, hint=hint, priority=priority)
