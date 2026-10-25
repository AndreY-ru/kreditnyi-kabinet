"""Проверка шансов до подачи: та же модель, что в конвейере, без БКИ и без записи в БД."""
from __future__ import annotations

from tests.helpers import LIQUIDATING, SV, YOUNG, company_id, draft, product_id
from tests.test_web import web_login


def _pre(c, inn: str, **body):
    return c.post(f"/api/companies/{company_id(c, inn)}/precheck", json={"amount": 1_000_000, "term_months": 12,
                                                                         **body})


def test_precheck_stable_company_high_without_db_writes(anna, db):
    from app.models import Application, ExternalCheck

    r = _pre(anna, SV)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["level"] == "high"
    assert body["products"][0]["verdict"] == "auto"
    assert any(f["title"] == "Кредитная история" and "с ваших слов" in f["text"] for f in body["factors"])
    assert "score" not in r.text and "points" not in r.text  # баллы и пороги клиенту не раскрываем
    assert db.query(Application).count() == 0 and db.query(ExternalCheck).count() == 0


def test_precheck_matches_real_pipeline_for_stable_company(anna):
    """Регрессия: без вопроса о просрочках проверка занижала оценку на 10 баллов и «Северный ветер»
    с прошлой заявкой (долг 3,5 млн) получал «средние шансы», хотя конвейер одобряет автоматически."""
    draft(anna, SV, "WORKING", 1_000_000, 12)
    body = _pre(anna, SV, amount=2_000_000, term_months=24).json()
    assert body["level"] == "high"
    assert {p["name"]: p["verdict"] for p in body["products"]}["Оборотный кредит"] == "auto"


def test_precheck_counts_monthly_payments_and_suggests_longer_term(anna):
    """3 млн на полгода: платежи с текущими кредитами (218 тыс. ₽ из прошлой заявки) больше прибыли.
    Та же сумма на два года проходит — срок влияет на решение, а не только на ставку."""
    draft(anna, SV, "WORKING", 1_000_000, 12)
    body = _pre(anna, SV, amount=3_000_000, term_months=6).json()
    pay = next(f for f in body["factors"] if f["title"] == "Ежемесячные платежи")
    assert pay["tone"] == "bad" and "вместе с текущими кредитами" in pay["text"]
    assert body["tips"][0].startswith("Комфортная сумма") and body["tips"][1].startswith("Возьмите на")
    assert "auto" not in {p["verdict"] for p in body["products"]}
    assert body["level"] == "medium"  # оценка высокая, но платежи больше прибыли — «шансы высокие» было бы неправдой
    # платёж по текущим кредитам клиент может указать сам: 20 тыс. ₽ вместо 218 тыс. — запас на 12 мес. есть
    def tone(body):
        return next(f for f in body["factors"] if f["title"] == "Ежемесячные платежи")["tone"]

    assert tone(_pre(anna, SV, amount=3_000_000).json()) == "bad"
    assert tone(_pre(anna, SV, amount=3_000_000, debt=3_500_000, monthly_payment=20_000).json()) == "ok"


def test_precheck_declared_current_overdue_is_low(anna):
    body = _pre(anna, SV, overdues="current").json()
    assert body["level"] == "low"
    assert any("текущая просрочка — погасите её" in t for t in body["tips"])


def test_precheck_young_company_points_to_start_product(anna):
    # 4 месяца работы: оборотный кредит не подходит по сроку, «Кредит на старт» — к специалисту
    body = _pre(anna, YOUNG, amount=300_000, revenue=3_000_000, profit=500_000, debt=0).json()
    verdicts = {p["name"]: p["verdict"] for p in body["products"]}
    assert verdicts["Кредит на старт бизнеса"] == "review"
    assert verdicts["Оборотный кредит"] == "unfit"


def test_precheck_liquidating_company_is_low(anna):
    assert _pre(anna, LIQUIDATING, revenue=3_000_000, profit=200_000).json()["level"] == "low"


def test_precheck_big_amount_suggests_comfortable_sum(anna):
    body = _pre(anna, SV, amount=25_000_000, term_months=36, revenue=84_600_000, profit=6_500_000,
                debt=3_500_000, goal="equipment").json()
    assert body["level"] == "low"
    assert body["comfortable_amount"] and body["comfortable_amount"] < 25_000_000
    assert body["tips"][0].startswith("Комфортная сумма")


def test_precheck_prefills_from_last_complete_application(anna, db):
    from app.models import Company
    from app.services.precheck import defaults

    draft(anna, SV, "WORKING", 1_000_000, 12)  # с финансами и обязательствами 3,5 млн
    r = anna.post("/api/applications", json={"company_id": company_id(anna, SV), "goal": "cash_gap",
                                             "product_id": product_id(anna, "OVERDRAFT"), "amount": 500_000,
                                             "term_months": 6})
    newest = r.json()["id"]  # самый свежий черновик — прибыль ещё не заполнена
    assert anna.put(f"/api/applications/{newest}/financials", json={"ebit": None, "liabilities": []}).status_code == 200
    d = defaults(db, db.query(Company).filter_by(inn=SV).one())
    assert d["debt"] == 3_500_000 and d["profit"] == 6_500_000 and d["source"].startswith("из заявки")


def test_check_page_and_live_results_render():
    c = web_login("anna@demo.local")
    assert 'href="/check"' in c.get("/cabinet").text  # кнопка на главной ведёт на проверку, а не в подбор
    page = c.get("/check")
    assert page.status_code == 200 and "Шансы высокие" in page.text
    part = c.post("/check/results", data={"goal": "working", "amount": "60 000 000", "term": "12", "trend": "FLAT",
                                          "revenue": "84 600 000", "profit": "6 500 000", "debt": "0", "guarantor": "1"})
    assert c.get("/check/results?revenue=84600000").status_code == 405  # финансы через URL не принимаем
    nojs = c.post("/check", data={"goal": "working", "amount": "3 000 000", "term": "12", "trend": "FLAT",
                                  "revenue": "84 600 000", "profit": "6 500 000", "debt": "0", "guarantor": "1"})
    assert nojs.status_code == 200 and "Шансы" in nojs.text  # форма без JavaScript тоже работает
    assert part.status_code == 200 and "Шансы" in part.text


def test_podbor_sends_current_form_to_check_including_collateral():
    """Кнопка «Оценить шансы» отправляет форму подбора как есть сейчас, а не ссылку, собранную при загрузке."""
    c = web_login("anna@demo.local")
    page = c.get("/podbor").text
    assert 'form="podbor-form" formaction="/check" formmethod="get"' in page
    assert '<div class="reveal"' in page  # блок залога под галочкой, раскрывается без JS
    r = c.get("/check", params={"goal": "working", "amount": "4 000 000", "term": "36", "collateral": "1",
                                "collateral_kind": "REAL_ESTATE", "collateral_value": "8 000 000", "guarantor": "1"})
    text = r.text.replace(" ", " ")
    assert 'id="cc-REAL_ESTATE" name="collateral_kind" value="REAL_ESTATE" checked' in text
    assert 'name="collateral_value" type="text" inputmode="numeric" data-money value="8 000 000"' in text


def test_podbor_opens_chances_in_a_dialog_on_the_same_page():
    """Оценка шансов из подбора — окном поверх страницы: подбор и всё введённое остаются на месте."""
    c = web_login("anna@demo.local")
    page = c.get("/podbor").text
    assert '<dialog id="chance-dialog"' in page and 'hx-post="/check/panel"' in page
    r = c.post("/check/panel", data={"goal": "working", "amount": "2 000 000", "term": "24", "collateral": "1",
                                     "collateral_kind": "VEHICLE", "collateral_value": "3 000 000", "guarantor": "1"})
    text = r.text.replace(" ", " ")
    assert r.status_code == 200 and "Шансы на одобрение" in text and "залог: транспорт" in text
    assert 'name="amount" value="2000000"' in text and 'name="collateral_kind" value="VEHICLE"' in text
    assert 'id="revenue"' in text and "Шансы высокие" in text  # финансы подставлены, оценка уже посчитана
    assert 'hx-target="#chance-results"' in text  # правки финансов пересчитываются внутри окна
