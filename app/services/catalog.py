"""Подбор продукта по цели, сумме, сроку и обеспечению (этап 1 процесса)."""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Company, Product

GOALS: dict[str, str] = {
    "working": "Пополнить оборотные средства",
    "equipment": "Купить оборудование",
    "cash_gap": "Закрыть кассовый разрыв",
    "new_direction": "Запустить новое направление",
    "refinance": "Рефинансировать кредит",
}

GOAL_PURPOSE: dict[str, str] = {
    "working": "пополнение оборотных средств",
    "equipment": "закупка оборудования",
    "cash_gap": "закрытие кассового разрыва",
    "new_direction": "запуск нового направления",
    "refinance": "рефинансирование кредита",
}

TERMS = [6, 12, 18, 24, 36, 60]


def _money(v: int) -> str:
    if v >= 1_000_000:
        s = f"{v / 1_000_000:.1f}".replace(".0", "").replace(".", ",")
        return f"{s} млн ₽"
    return f"{v // 1000} тыс. ₽"


@dataclass
class Match:
    product: Product
    fit: int
    eligible: bool
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def match_products(
    db: Session,
    *,
    goal: str,
    amount: int,
    term: int,
    has_collateral: bool,
    has_guarantor: bool,
    company: Company | None = None,
    limit: int = 3,
) -> list[Match]:
    products = db.scalars(select(Product).where(Product.active.is_(True)).order_by(Product.id)).all()
    months = company.business_months() if company else None
    out: list[Match] = []
    for p in products:
        m = Match(product=p, fit=0, eligible=True)
        if goal in (p.goals or []):
            m.fit += 3
            m.reasons.append(f"Цель «{GOAL_PURPOSE.get(goal, goal)}» подходит")
        else:
            m.fit -= 2
        if p.min_amount <= amount <= p.max_amount:
            m.fit += 2
            m.reasons.append("Сумма в пределах лимита")
        else:
            m.eligible = False
            m.warnings.append(f"Сумма должна быть от {_money(p.min_amount)} до {_money(p.max_amount)}")
        if p.min_term <= term <= p.max_term:
            m.fit += 1
        else:
            m.eligible = False
            m.warnings.append(f"Срок от {p.min_term} до {p.max_term} мес.")
        if p.requires_collateral and not has_collateral:
            m.fit -= 2
            m.warnings.append("Нужен залог — вы его не отметили")
        elif p.unsecured_max and amount > p.unsecured_max and not has_collateral:
            m.fit -= 1
            m.warnings.append(f"Без залога — до {_money(p.unsecured_max)}")
        elif not p.requires_collateral:
            m.reasons.append("Залог не нужен" + (", достаточно поручительства собственника" if p.requires_guarantor else ""))
        if p.requires_guarantor and not has_guarantor:
            m.fit -= 1
            m.warnings.append("Нужно поручительство собственника")
        if months is not None and months < p.min_business_months:
            m.eligible = False
            m.warnings.append(f"Компания должна работать не меньше {p.min_business_months} мес.")
        if p.gov_support:
            m.reasons.append("Льготная ставка по госпрограмме")
        out.append(m)
    out.sort(key=lambda m: (not m.eligible, -m.fit))
    return out[:limit]


def alternatives_for(db: Session, product: Product, company: Company, amount: int) -> list[Product]:
    """Другие продукты, под которые компания проходит по сроку бизнеса (для отказа по продукту)."""
    months = company.business_months() or 0
    products = db.scalars(select(Product).where(Product.active.is_(True), Product.id != product.id)).all()
    res = [p for p in products if p.min_business_months <= months and p.min_amount <= amount]
    res.sort(key=lambda p: abs(p.max_amount - amount))
    return res[:2]
