"""Профиль компании: автозаполнение по ИНН из мока ЕГРЮЛ/ЕГРИП (этап 2)."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.transitions import DomainError, Forbidden, NotFound, ValidationFailed
from app.models import AuditLog, Company, CompanyMember, User, utcnow
from app.services import integrations


def inn_is_valid(inn: str) -> bool:
    if not inn.isdigit() or len(inn) not in (10, 12):
        return False
    d = [int(c) for c in inn]
    if len(inn) == 10:
        k = [2, 4, 10, 3, 5, 9, 4, 6, 8]
        return sum(a * b for a, b in zip(d, k)) % 11 % 10 == d[9]
    k1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    k2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    return (sum(a * b for a, b in zip(d, k1)) % 11 % 10 == d[10]
            and sum(a * b for a, b in zip(d, k2)) % 11 % 10 == d[11])


def apply_egrul(company: Company, data: dict) -> None:
    company.kind = data.get("kind", company.kind or "OOO")
    company.ogrn = data.get("ogrn")
    company.name = data.get("name", company.name)
    if data.get("registered_at"):
        company.registered_at = date.fromisoformat(data["registered_at"])
    company.status = data.get("status", "ACTIVE")
    company.address = data.get("address")
    company.okved = data.get("okved")
    company.okved_name = data.get("okved_name")
    company.director = data.get("director")
    company.founders = data.get("founders")
    company.egrul_fetched_at = utcnow()


def user_companies(db: Session, user: User) -> list[Company]:
    return list(db.scalars(
        select(Company).join(CompanyMember).where(CompanyMember.user_id == user.id).order_by(Company.id)
    ).all())


def membership(db: Session, user: User, company_id: int) -> CompanyMember | None:
    return db.scalar(select(CompanyMember).where(CompanyMember.user_id == user.id,
                                                 CompanyMember.company_id == company_id))


def require_member(db: Session, user: User, company_id: int, *, apply: bool = False) -> CompanyMember:
    m = membership(db, user, company_id)
    if m is None:
        raise NotFound("Компания не найдена")
    if apply and not m.can_apply:
        raise Forbidden("У вас нет права оформлять заявки от имени компании")
    return m


def add_company_by_inn(db: Session, user: User, inn: str, position: str, authority: str | None = None) -> Company:
    inn = inn.strip()
    if not inn_is_valid(inn):
        raise ValidationFailed("Проверьте ИНН: 10 цифр для организации, 12 для ИП", code="inn_invalid")
    company = db.scalar(select(Company).where(Company.inn == inn))
    if company is None:
        res = integrations.fns_egrul(inn)
        if res.http_status == 404:
            raise NotFound("Компания с таким ИНН не найдена в реестре")
        if not res.ok or res.response is None:
            raise DomainError("Реестр ФНС сейчас недоступен, попробуйте через минуту", code="fns_unavailable")
        company = Company(inn=inn, kind="OOO", name="")
        apply_egrul(company, res.response)
        t = integrations.fns_transparent(inn)
        if t.ok and t.response:
            company.employees = t.response.get("employees")
        db.add(company)
        db.flush()
    if membership(db, user, company.id) is None:
        db.add(CompanyMember(user_id=user.id, company_id=company.id, position=position or "Представитель",
                             can_apply=True, authority=authority))
        db.add(AuditLog(actor_id=user.id, action="company.link", entity="company", entity_id=company.id,
                        data={"inn": inn}))
    db.flush()
    return company


def self_check(company: Company) -> dict:
    """Предварительная самопроверка по частым причинам отказа. В кредитную историю не попадает."""
    items = []
    months = company.business_months()
    if months is not None:
        years = months // 12
        disp = f"{years} лет" if years >= 5 else (f"{years} г." if years else f"{months} мес.")
        ok = months >= 9
        items.append({"title": "Срок работы бизнеса", "ok": ok,
                      "text": f"{disp} — больше минимума продуктов" if ok else f"{disp} — меньше минимума большинства продуктов"})
    st_ok = company.status == "ACTIVE"
    items.append({"title": "Статус компании", "ok": st_ok,
                  "text": "Действует, нет ликвидации и реорганизации" if st_ok else "В реестре отмечена ликвидация или реорганизация"})
    t = integrations.fns_transparent(company.inn)
    if t.ok and t.response is not None:
        arrears = int(t.response.get("tax_arrears") or 0)
        items.append({"title": "Налоговая задолженность", "ok": arrears == 0,
                      "text": "Не найдена" if arrears == 0 else f"Найдена недоимка {arrears:,} ₽ — погасите до подачи".replace(",", " ")})
        source_ok = True
    else:
        items.append({"title": "Налоговая задолженность", "ok": None, "text": "Сервис ФНС не ответил — проверим после подачи"})
        source_ok = False
    items.append({"title": "Долговая нагрузка", "ok": None,
                  "text": "Проверим после заявки — укажите действующие кредиты"})
    return {"items": items, "source_ok": source_ok}
