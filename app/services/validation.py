"""Проверка полноты и корректности заявки (этап 6). Используется и до отправки, и в конвейере."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Application, Consent, utcnow


def _money(v: int) -> str:
    return f"{v:,}".replace(",", " ") + " ₽"


def active_consent(db: Session, app: Application, kind: str) -> Consent | None:
    rows = db.scalars(
        select(Consent)
        .where(Consent.application_id == app.id, Consent.kind == kind, Consent.revoked_at.is_(None))
        .order_by(Consent.given_at.desc())
    ).all()
    now = utcnow()
    for c in rows:
        if c.is_active(now):
            return c
    return None


def validate(db: Session, app: Application) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []

    def err(step: str, field: str, message: str) -> None:
        errors.append({"step": step, "field": field, "message": message})

    p = app.product
    if not (p.min_amount <= app.amount <= p.max_amount):
        err("params", "amount", f"Сумма для «{p.name}»: от {_money(p.min_amount)} до {_money(p.max_amount)}")
    if not (p.min_term <= app.term_months <= p.max_term):
        err("params", "term_months", f"Срок для «{p.name}»: от {p.min_term} до {p.max_term} мес.")
    if p.requires_collateral and not app.has_collateral:
        err("params", "has_collateral", f"Для «{p.name}» нужен залог")
    if p.requires_guarantor and not app.has_guarantor and app.company.kind == "OOO":
        err("params", "has_guarantor", "Нужно поручительство собственника бизнеса")

    f = app.financials
    if f is None:
        err("finance", "revenue", "Заполните финансовые показатели")
    else:
        if f.revenue is None or f.revenue < 0:
            err("finance", "revenue", "Укажите выручку за последние 12 месяцев")
        if f.expenses is None or f.expenses < 0:
            err("finance", "expenses", "Укажите расходы")
        if f.ebit is None:
            err("finance", "ebit", "Укажите прибыль до процентов и налогов. Если убыток — с минусом")
        if f.amortization is None or f.amortization < 0:
            err("finance", "amortization", "Укажите сумму. Если амортизации нет, поставьте 0")
        if f.revenue_trend not in ("GROWTH", "FLAT", "DECLINE"):
            err("finance", "revenue_trend", "Выберите, как менялась выручка")
        if f.revenue is not None and f.expenses is not None and f.ebit is not None and f.ebit > f.revenue:
            err("finance", "ebit", "Прибыль не может быть больше выручки")
    for i, liab in enumerate(app.liabilities):
        if not liab.creditor or liab.balance is None or liab.balance < 0:
            err("finance", f"liabilities.{i}", f"Проверьте обязательство №{i + 1}: кредитор и остаток долга")

    if active_consent(db, app, "PD") is None:
        err("consents", "consent_pd", "Нужно согласие на обработку персональных данных")
    if active_consent(db, app, "BKI") is None:
        err("consents", "consent_bki", "Нужно действующее согласие на запрос кредитной истории")
    if app.has_guarantor and app.company.kind == "OOO" and active_consent(db, app, "BKI_GUARANTOR") is None:
        err("consents", "consent_guarantor", "Нужно согласие поручителя на запрос его кредитной истории")
    return errors
