"""Демо-данные: продукты, скоринговая модель, пользователи, компании и заявки в разных статусах.

ВСЕ ДАННЫЕ СИНТЕТИЧЕСКИЕ. Запуск: python -m app.seed (идемпотентно: повторный запуск ничего не дублирует).
Требует запущенного мока ФНС (компании заполняются по ИНН через него).
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal, get_engine
from app.models import Company, CompanyMember, Product, ScoringModel, User, clock_shift
from app.security import hash_password
from app.services import analyst as an
from app.services import applications as svc
from app.services.companies import add_company_by_inn, add_member
from app.services.scoring import DEFAULT_MODEL
from app.worker import run_pending

log = logging.getLogger("seed")
PASSWORD = "demo"

PRODUCTS = [
    dict(code="WORKING", name="Оборотный кредит", kind="LOAN",
         description="Пополнение оборотных средств без залога, достаточно поручительства собственника.",
         goals=["working", "cash_gap"], min_amount=500_000, max_amount=10_000_000, min_term=3, max_term=36,
         min_business_months=9, requires_collateral=False, unsecured_max=None, requires_guarantor=True,
         base_rate=Decimal("18.50"), auto_limit=3_000_000, decision_days=2, gov_support=False),
    dict(code="CREDIT_LINE", name="Кредитная линия", kind="CREDIT_LINE",
         description="Можно брать деньги частями по мере надобности. Без залога — до 10 млн ₽.",
         goals=["working", "equipment", "cash_gap"], min_amount=1_000_000, max_amount=60_000_000, min_term=6,
         max_term=60, min_business_months=12, requires_collateral=False, unsecured_max=10_000_000,
         requires_guarantor=True, base_rate=Decimal("17.90"), auto_limit=10_000_000, decision_days=5,
         gov_support=False),
    dict(code="OVERDRAFT", name="Овердрафт", kind="OVERDRAFT",
         description="Покрывает кассовые разрывы автоматически, когда на счёте не хватает денег.",
         goals=["cash_gap", "working"], min_amount=100_000, max_amount=20_000_000, min_term=1, max_term=12,
         min_business_months=6, requires_collateral=False, unsecured_max=None, requires_guarantor=False,
         base_rate=Decimal("19.90"), auto_limit=2_000_000, decision_days=1, gov_support=False),
    dict(code="DEVELOPMENT", name="Кредит на развитие", kind="LOAN",
         description="На запуск нового направления или расширение бизнеса, без залога.",
         goals=["new_direction", "equipment", "refinance"], min_amount=1_000_000, max_amount=30_000_000,
         min_term=12, max_term=60, min_business_months=9, requires_collateral=False, unsecured_max=None,
         requires_guarantor=True, base_rate=Decimal("19.50"), auto_limit=5_000_000, decision_days=2,
         gov_support=False),
    dict(code="GOV_SUPPORT", name="Кредит с господдержкой", kind="LOAN",
         description="Ставка ниже рыночной, компания должна подходить под требования программы.",
         goals=["working", "equipment", "new_direction"], min_amount=1_000_000, max_amount=50_000_000,
         min_term=12, max_term=36, min_business_months=12, requires_collateral=False, unsecured_max=None,
         requires_guarantor=True, base_rate=Decimal("16.50"), auto_limit=0, decision_days=10, gov_support=True),
    dict(code="START", name="Кредит на старт бизнеса", kind="LOAN",
         description="Для компаний и ИП от 3 месяцев работы: небольшая сумма под поручительство собственника.",
         goals=["working", "equipment", "new_direction", "cash_gap"], min_amount=300_000, max_amount=3_000_000,
         min_term=6, max_term=24, min_business_months=3, requires_collateral=False, unsecured_max=None,
         requires_guarantor=True, base_rate=Decimal("21.50"), auto_limit=0, decision_days=3, gov_support=False),
]

USERS = [
    # email, ФИО, роль, должность
    ("anna@demo.local", "Анна Петрова", "CLIENT", None),
    ("igor@demo.local", "Игорь Смирнов", "CLIENT", None),
    ("elena@demo.local", "Елена Козлова", "CLIENT", None),
    ("orlova@demo.local", "Мария Орлова", "CLIENT", None),
    ("kim@demo.local", "Денис Ким", "CLIENT", None),
    ("zakharov@demo.local", "Олег Захаров", "CLIENT", None),
    ("maria@bank.local", "Мария Котова", "ANALYST", "Кредитный аналитик"),
    ("petr@bank.local", "Пётр Волков", "ANALYST", "Кредитный аналитик"),
    ("admin@bank.local", "Админ Системы", "ADMIN", "Администратор"),
]

MEMBERS = [
    # email, ИНН, должность, может оформлять, может подписывать, полномочия.
    # В «Северном ветре» Анна готовит заявки, а подписывает директор Игорь (J5). В компаниях на аутсорсе
    # она единственный участник, поэтому подписывает по доверенности — иначе договор было бы некому подписать.
    ("anna@demo.local", "7701234560", "Финансовый директор", True, False, "Доверенность № 14 до 31.12.2026"),
    ("igor@demo.local", "7701234560", "Генеральный директор", True, True, "Генеральный директор (ЕГРЮЛ)"),
    ("anna@demo.local", "7801555014", "Бухгалтер на аутсорсе", True, True, "Доверенность № 3 с правом подписи"),
    ("anna@demo.local", "5001777021", "Бухгалтер на аутсорсе", True, True, "Доверенность № 7 с правом подписи"),
    ("anna@demo.local", "6022333043", "Бухгалтер на аутсорсе", True, True, "Доверенность № 9 с правом подписи"),
    ("anna@demo.local", "4703444053", "Бухгалтер на аутсорсе", True, True, "Доверенность № 11 с правом подписи"),
    ("elena@demo.local", "772012345670", "Индивидуальный предприниматель", True, True, "ИП"),
    ("orlova@demo.local", "5401987658", "Генеральный директор", True, True, "Генеральный директор (ЕГРЮЛ)"),
    ("kim@demo.local", "7724112230", "Генеральный директор", True, True, "Генеральный директор (ЕГРЮЛ)"),
    ("zakharov@demo.local", "6658445560", "Генеральный директор", True, True, "Генеральный директор (ЕГРЮЛ)"),
]


def _user(db: Session, email: str) -> User:
    u = db.scalar(select(User).where(User.email == email))
    assert u is not None, email
    return u


def _company(db: Session, inn: str) -> Company:
    c = db.scalar(select(Company).where(Company.inn == inn))
    assert c is not None, inn
    return c


def _product(db: Session, code: str) -> Product:
    p = db.scalar(select(Product).where(Product.code == code))
    assert p is not None, code
    return p


def seed_reference(db: Session) -> None:
    for spec in PRODUCTS:
        if db.scalar(select(Product).where(Product.code == spec["code"])) is None:
            db.add(Product(**spec))
    if db.scalar(select(ScoringModel).where(ScoringModel.version == DEFAULT_MODEL["version"])) is None:
        # новая версия карты приходит вместе с кодом: прежние остаются в истории, активной становится новая
        for old in db.scalars(select(ScoringModel)).all():
            old.active = False
        db.add(ScoringModel(version=DEFAULT_MODEL["version"], config=DEFAULT_MODEL, active=True))
    for email, name, role, position in USERS:
        if db.scalar(select(User).where(User.email == email)) is None:
            db.add(User(email=email, full_name=name, role=role, position=position,
                        password_hash=hash_password(PASSWORD)))
    db.commit()
    for email, inn, position, can_apply, can_sign, authority in MEMBERS:
        user = _user(db, email)
        company = db.scalar(select(Company).where(Company.inn == inn))
        if company is None:
            company = add_company_by_inn(db, user, inn, position, authority)
        m = db.scalar(select(CompanyMember).where(CompanyMember.user_id == user.id,
                                                  CompanyMember.company_id == company.id))
        if m is None:  # второй и следующие участники: в кабинете их добавляет руководитель
            m = add_member(db, company, user, position=position)
        m.can_apply, m.can_sign, m.authority, m.position = can_apply, can_sign, authority, position
    db.commit()


def _draft(db: Session, email: str, inn: str, code: str, amount: int, term: int, goal: str, *,
           revenue: int, expenses: int, ebit: int, amort: int, trend: str, liabs: list[tuple],
           collateral: tuple[str, int, str] | None = None, guarantor: bool = True):
    user, company, product = _user(db, email), _company(db, inn), _product(db, code)
    app = svc.create_draft(db, user, company, product, amount=amount, term_months=term, goal=goal,
                           has_collateral=collateral is not None, has_guarantor=guarantor)
    if collateral:
        kind, value, desc = collateral
        svc.update_params(db, app, collateral_kind=kind, collateral_value=value, collateral_desc=desc)
    svc.update_financials(db, app, {"revenue": revenue, "expenses": expenses, "ebit": ebit, "amortization": amort,
                                    "revenue_trend": trend},
                          [{"creditor": c, "balance": b, "monthly_payment": p, "maturity": m} for c, b, p, m in liabs])
    return user, app


def _submit(db: Session, user: User, app) -> None:
    for kind in ("PD", "BKI") + (("BKI_GUARANTOR",) if app.has_guarantor and app.company.kind == "OOO" else ()):
        svc.give_consent(db, app, user, kind)
    svc.submit(db, app, user)
    db.commit()
    run_pending(db)
    db.refresh(app)


SV_LIABS = [("Банк А · кредит", 2_400_000, 160_000, "08.2027"), ("Лизинговая компания · грузовик", 1_100_000, 58_000, "03.2028")]


def seed_applications(db: Session) -> None:
    from app.models import Application
    if db.scalar(select(Application)) is not None:
        log.info("Заявки уже есть — пропускаем")
        return
    maria = _user(db, "maria@bank.local")
    sv = dict(revenue=84_600_000, expenses=76_900_000, ebit=6_200_000, amort=300_000, trend="GROWTH", liabs=SV_LIABS)
    # Заявки «живут» в последние трое суток (часов назад), иначе на странице «Процесс» время на этапах ≈ 0 с.

    # 1. Овердрафт: авто-одобрение → выдан. Готовит Анна, подписывает Игорь (право подписи)
    with _ago(72):
        anna, a1 = _draft(db, "anna@demo.local", "7701234560", "OVERDRAFT", 1_500_000, 12, "cash_gap",
                          guarantor=False, **sv)
    with _ago(71.6):
        _submit(db, anna, a1)
    with _ago(70.9):
        svc.accept_offer(db, a1, anna)
        db.commit()
    with _ago(68):
        svc.sign(db, a1, _user(db, "igor@demo.local"))
        db.commit()

    # 2. Кредитная линия 12 млн: ручной разбор (выше авто-лимита и лимита без залога), у Марии
    with _ago(5.5):
        anna, a2 = _draft(db, "anna@demo.local", "7701234560", "CREDIT_LINE", 12_000_000, 36, "equipment", **sv)
    with _ago(5):
        _submit(db, anna, a2)
    with _ago(4.6):
        an.take(db, a2, maria)
        db.commit()

    # 3. Оборотный 5 млн: ручной разбор → запрос данных (баннер на главной клиента)
    with _ago(28):
        anna, a3 = _draft(db, "anna@demo.local", "7701234560", "WORKING", 5_000_000, 24, "working", **sv)
    with _ago(27.5):
        _submit(db, anna, a3)
    with _ago(26):
        an.take(db, a3, maria)
    with _ago(24.8):
        an.decide(db, a3, maria, kind="info", expected_version=a3.version,
                  comment="Резкий рост выручки, нужно подтвердить расчёты с покупателями",
                  doc_kind="Оборотно-сальдовая ведомость по счёту 62",
                  info_comment="Выручка за год выросла заметно сильнее, чем в прошлых периодах. Ведомость поможет подтвердить расчёты с покупателями.")
        db.commit()

    # 4. Черновик: кредит на развитие
    with _ago(2):
        _draft(db, "anna@demo.local", "7701234560", "DEVELOPMENT", 3_000_000, 18, "new_direction",
               revenue=84_600_000, expenses=None, ebit=None, amort=None, trend=None, liabs=SV_LIABS)  # type: ignore[arg-type]
        db.commit()

    # 5. Пекарня: налоговая недоимка → ручной разбор, не назначена; срок разбора уже истёк (подсветка SLA)
    with _ago(9.5):
        orlova, a5 = _draft(db, "orlova@demo.local", "5401987658", "WORKING", 2_500_000, 18, "working",
                            revenue=41_000_000, expenses=37_500_000, ebit=3_400_000, amort=500_000, trend="FLAT",
                            liabs=[("Банк Б", 900_000, 75_000, "12.2026")])
    with _ago(9.2):
        _submit(db, orlova, a5)

    # 6. Технопарк: крупная кредитная линия → ручной разбор, не назначена
    with _ago(3.4):
        kim, a6 = _draft(db, "kim@demo.local", "7724112230", "CREDIT_LINE", 18_000_000, 48, "equipment",
                         revenue=260_000_000, expenses=231_000_000, ebit=27_000_000, amort=4_000_000, trend="GROWTH",
                         liabs=[("Банк В", 14_000_000, 690_000, "05.2028")],
                         collateral=("REAL_ESTATE", 26_000_000, "Производственный корпус в Хабаровске, 2 400 м²"))
    with _ago(3):
        _submit(db, kim, a6)

    # 7. СтройМонтаж: господдержка — всегда специалист
    with _ago(1.8):
        zah, a7 = _draft(db, "zakharov@demo.local", "6658445560", "GOV_SUPPORT", 7_000_000, 36, "equipment",
                         revenue=96_000_000, expenses=88_000_000, ebit=8_500_000, amort=1_200_000, trend="GROWTH",
                         liabs=[("Банк Г", 3_000_000, 140_000, "11.2027")])
    with _ago(1.5):
        _submit(db, zah, a7)

    # 8. ИП Козлова: овердрафт, авто-одобрение (ждёт решения клиента)
    with _ago(20.3):
        elena, a8 = _draft(db, "elena@demo.local", "772012345670", "OVERDRAFT", 800_000, 12, "cash_gap",
                           guarantor=False, revenue=9_500_000, expenses=7_900_000, ebit=1_500_000, amort=100_000,
                           trend="GROWTH", liabs=[])
    with _ago(20):
        _submit(db, elena, a8)
    db.commit()


def _ago(hours: float):
    return clock_shift(-timedelta(hours=hours))


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings.pipeline_step_delay = 0
    settings.enrich_retry_delay = 0
    get_engine()
    with SessionLocal() as db:
        seed_reference(db)
        seed_applications(db)
    log.info("Демо-данные готовы. Пароль у всех пользователей: %s", PASSWORD)


if __name__ == "__main__":
    main()
