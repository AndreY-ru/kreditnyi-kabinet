"""Профиль компании: автозаполнение по ИНН из мока ЕГРЮЛ/ЕГРИП (этап 2)."""
from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.transitions import DomainError, Forbidden, NotFound, ValidationFailed
from app.models import AuditLog, Company, CompanyMember, Product, User, utcnow
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


def add_member(db: Session, company: Company, user: User, *, position: str, can_apply: bool = True,
               can_sign: bool = False, authority: str | None = None, actor: User | None = None) -> CompanyMember:
    m = CompanyMember(user_id=user.id, company_id=company.id, position=position or "Представитель",
                      can_apply=can_apply, can_sign=can_sign, authority=authority)
    db.add(m)
    db.add(AuditLog(actor_id=(actor or user).id, action="company.member_add", entity="company", entity_id=company.id,
                    data={"user": user.email, "can_apply": can_apply, "can_sign": can_sign}))
    db.flush()
    return m


def add_company_by_inn(db: Session, user: User, inn: str, position: str, authority: str | None = None) -> Company:
    """Подключить компанию к кабинету.

    Первый подключивший становится администратором компании в кабинете (в реальном банке полномочия
    подтверждаются ЭП или доверенностью — здесь смоделировано). Остальных сотрудников добавляет
    руководитель с правом подписи (invite_member): иначе любой клиент получил бы доступ к чужим заявкам по ИНН.
    """
    inn = inn.strip()
    if not inn_is_valid(inn):
        raise ValidationFailed("Проверьте ИНН: 10 цифр для организации, 12 для ИП", code="inn_invalid")
    company = db.scalar(select(Company).where(Company.inn == inn))
    if company is not None:
        if membership(db, user, company.id) is not None:
            return company
        if db.scalar(select(CompanyMember.id).where(CompanyMember.company_id == company.id).limit(1)) is not None:
            raise Forbidden("Компания уже подключена к кабинету. Доступ выдаёт руководитель: попросите его "
                            "добавить вас в разделе «Компания».", code="company_taken")
    else:
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
    add_member(db, company, user, position=position, can_apply=True, can_sign=True,
               authority=authority or "Полномочия подтверждены при подключении (смоделировано)")
    return company


def invite_member(db: Session, inviter: User, company_id: int, *, email: str, position: str,
                  can_apply: bool, can_sign: bool) -> CompanyMember:
    """Руководитель добавляет коллегу в команду компании (J5: заявку готовит бухгалтер, подписывает директор)."""
    m = require_member(db, inviter, company_id)
    if not m.can_sign:
        raise Forbidden("Добавлять сотрудников может руководитель с правом подписи", code="no_sign_right")
    target = db.scalar(select(User).where(User.email == email.strip().lower()))
    if target is None or target.role != "CLIENT":
        raise NotFound("Пользователь с таким email не зарегистрирован в кабинете", code="user_not_found")
    if membership(db, target, company_id) is not None:
        raise ValidationFailed(f"{target.full_name} уже в команде", code="already_member")
    company = db.get(Company, company_id)
    assert company is not None
    return add_member(db, company, target, position=position, can_apply=can_apply, can_sign=can_sign,
                      authority=f"Добавлен(а) руководителем: {inviter.full_name}", actor=inviter)


def self_check(db: Session, company: Company) -> dict:
    """Предварительная самопроверка по частым причинам отказа. В кредитную историю не попадает."""
    items = []
    months = company.business_months()
    if months is not None:
        years = months // 12
        disp = f"{years} лет" if years >= 5 else (f"{years} г." if years else f"{months} мес.")
        min_months = db.scalar(select(func.min(Product.min_business_months)).where(Product.active.is_(True))) or 0
        ok = months >= min_months
        items.append({"title": "Срок работы бизнеса", "ok": ok,
                      "text": f"{disp} — подходит для продуктов банка" if ok else f"{disp} — меньше минимума всех продуктов"})
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
