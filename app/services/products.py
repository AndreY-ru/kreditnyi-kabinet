"""Каталог продуктов: создание и правка администратором.

Удаления нет: на продукт ссылаются заявки, а снимок заявки хранит код и версию продукта. Снятый продукт
уходит в архив (active=False) и пропадает из подбора и проверки шансов; черновики на нём не отправить.
Каждая правка поднимает версию и пишется в журнал с тем, что было и что стало.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.transitions import ValidationFailed
from app.models import AuditLog, Product, User
from app.services.catalog import GOALS

KINDS = {"LOAN": "Кредит", "CREDIT_LINE": "Кредитная линия", "OVERDRAFT": "Овердрафт"}
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{1,39}$")
EDITABLE = ("name", "kind", "description", "goals", "min_amount", "max_amount", "min_term", "max_term",
            "min_business_months", "requires_collateral", "unsecured_max", "requires_guarantor", "base_rate",
            "auto_limit", "decision_days", "gov_support", "active")


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    digits = re.sub(r"[^\d]", "", str(value))[:16]
    return int(digits) if digits else None


def _bool(value: Any) -> bool:
    return value in (True, 1, "1", "true", "on", "yes")


def as_dict(p: Product) -> dict[str, Any]:
    return {k: getattr(p, k) for k in ("code",) + EDITABLE}


def clean(data: dict[str, Any], *, creating: bool) -> dict[str, Any]:
    """Проверка и нормализация полей. Ошибки — по полям, понятным языком."""
    err: dict[str, str] = {}
    out: dict[str, Any] = {}
    out["name"] = " ".join(str(data.get("name") or "").split())
    if not 2 <= len(out["name"]) <= 120:
        err["name"] = "Название — от 2 до 120 символов"
    if creating:
        out["code"] = str(data.get("code") or "").strip().upper()
        if not _CODE.match(out["code"]):
            err["code"] = "Код — латиница в верхнем регистре, цифры и _, например SEASON_LOAN"
    out["kind"] = data.get("kind") if data.get("kind") in KINDS else None
    if out["kind"] is None:
        err["kind"] = "Выберите вид продукта"
    out["description"] = str(data.get("description") or "").strip()
    if len(out["description"]) > 1000:
        err["description"] = "Описание — до 1000 символов"
    goals = data.get("goals") or []
    out["goals"] = [g for g in GOALS if g in goals]  # порядок — как в подборе, чужие значения отбрасываем
    if not out["goals"]:
        err["goals"] = "Отметьте хотя бы одну цель"

    for key in ("min_amount", "max_amount", "min_term", "max_term", "min_business_months", "auto_limit",
                "decision_days", "unsecured_max"):
        out[key] = _int(data.get(key))
    lo, hi = out["min_amount"], out["max_amount"]
    if lo is None or not 10_000 <= lo <= 10**12:
        err["min_amount"] = "Минимальная сумма — от 10 000 ₽"
    if hi is None or not 10_000 <= hi <= 10**12:
        err["max_amount"] = "Максимальная сумма — до 1 трлн ₽"
    elif lo is not None and lo > hi:
        err["max_amount"] = "Максимальная сумма меньше минимальной"
    t1, t2 = out["min_term"], out["max_term"]
    if t1 is None or not 1 <= t1 <= 120:
        err["min_term"] = "Срок — от 1 до 120 мес."
    if t2 is None or not 1 <= t2 <= 120:
        err["max_term"] = "Срок — от 1 до 120 мес."
    elif t1 is not None and t1 > t2:
        err["max_term"] = "Максимальный срок меньше минимального"
    if out["min_business_months"] is None or not 0 <= out["min_business_months"] <= 240:
        err["min_business_months"] = "Срок работы бизнеса — от 0 до 240 мес."
    if out["decision_days"] is None or not 1 <= out["decision_days"] <= 60:
        err["decision_days"] = "Срок решения — от 1 до 60 дней"
    out["auto_limit"] = out["auto_limit"] or 0
    if hi is not None and out["auto_limit"] > hi:
        err["auto_limit"] = "Авто-лимит не может быть больше максимальной суммы"

    for key in ("requires_collateral", "requires_guarantor", "gov_support", "active"):
        out[key] = _bool(data.get(key))
    if out["unsecured_max"] is not None:
        if out["requires_collateral"]:
            err["unsecured_max"] = "Если залог обязателен, лимита без залога быть не может"
        elif lo is not None and hi is not None and not lo <= out["unsecured_max"] <= hi:
            err["unsecured_max"] = "Лимит без залога — между минимальной и максимальной суммой"

    try:
        rate = Decimal(str(data.get("base_rate") or "").replace(",", ".").strip())
        if not rate.is_finite() or not Decimal("0") < rate < Decimal("100"):
            raise InvalidOperation
        out["base_rate"] = rate.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        err["base_rate"] = "Ставка — число от 0 до 100, например 18,5"

    if err:
        raise ValidationFailed("Проверьте поля продукта", code="product_invalid", details=err)
    return out


def save_product(db: Session, user: User, data: dict[str, Any], product: Product | None = None) -> Product:
    values = clean(data, creating=product is None)
    if product is None:
        if db.scalar(select(Product.id).where(Product.code == values["code"])) is not None:
            raise ValidationFailed("Проверьте поля продукта", code="product_invalid",
                                   details={"code": "Продукт с таким кодом уже есть"})
        product = Product(**values, version=1)
        db.add(product)
        db.flush()
        db.add(AuditLog(actor_id=user.id, action="product.create", entity="product", entity_id=product.id,
                        data={"code": product.code, "name": product.name}))
        return product
    if set(values["goals"]) == set(product.goals or []):
        values["goals"] = product.goals  # тот же набор целей в другом порядке — не изменение
    changes = {k: [str(getattr(product, k)), str(v)] for k, v in values.items() if getattr(product, k) != v}
    if changes:
        for k, v in values.items():
            setattr(product, k, v)
        product.version += 1
        db.add(AuditLog(actor_id=user.id, action="product.update", entity="product", entity_id=product.id,
                        data={"version": product.version, "changes": changes}))
    db.flush()
    return product
