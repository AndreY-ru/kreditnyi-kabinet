"""Ввод: где есть готовый список — выбираем из него, числа и строки — в разумных пределах.

Раньше в должность можно было вписать что угодно, а слишком длинная строка или число с двадцатью цифрами
роняли запрос с 500 на PostgreSQL (VARCHAR и BIGINT). Теперь — понятная ошибка у поля.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from tests.helpers import SV, consent_all, draft, submit_and_process
from tests.test_web import web_login

NEW_CO = "7713550120"


def test_position_only_from_list(anna):
    assert anna.post("/api/companies", json={"inn": NEW_CO, "position": "Царь и бог"}).status_code == 422
    cid = anna.post("/api/companies", json={"inn": NEW_CO}).json()["id"]
    members = f"/api/companies/{cid}/members"
    assert anna.post(members, json={"email": "elena@demo.local", "position": "Бог"}).status_code == 422
    r = anna.post(members, json={"email": "elena@demo.local", "position": "Главный бухгалтер"})
    assert r.status_code == 201 and r.json()["position"] == "Главный бухгалтер"


def test_web_invite_offers_list_and_rejects_custom_position():
    c = web_login("igor@demo.local")
    page = c.get("/company").text
    assert '<select class="sel" id="inv-pos" name="position">' in page and "<option>Юрист</option>" in page
    r = c.post("/company/invite", data={"email": "elena@demo.local", "position": "Бог"})
    assert r.status_code == 422 and "Выберите должность из списка" in r.text
    r = c.post("/company/invite", data={"email": "elena@demo.local", "position": "Главный бухгалтер"})
    assert r.status_code == 200 and "Елена Козлова" in r.text


def test_absurd_numbers_are_validation_errors(anna):
    app = draft(anna, SV, "WORKING", 1_000_000, 12)
    assert anna.put(f"/api/applications/{app['id']}/financials",
                    json={"revenue": 10**19, "liabilities": []}).status_code == 422
    r = anna.post("/api/applications", json={"company_id": app["company_id"], "product_id": app["product_id"],
                                             "amount": 10**19, "term_months": 12, "goal": "working"})
    assert r.status_code == 422 and r.json()["error"] == "number_too_large"

    c = web_login("anna@demo.local")
    r = c.post(f"/applications/{app['id']}/edit", data={"revenue": "9" * 5000, "action": "save"})
    assert r.status_code == 422 and "Слишком большое число в поле «Выручка»" in r.text
    r = c.post(f"/applications/{app['id']}/edit", data={"employees": "99999999999", "action": "save"})
    assert r.status_code == 422 and "«Сотрудники»" in r.text


def test_liability_maturity_checked_and_errors_shown_next_to_table(anna, db):
    from app.models import Liability

    app = draft(anna, SV, "WORKING", 1_000_000, 12)
    c = web_login("anna@demo.local")
    r = c.post(f"/applications/{app['id']}/edit", data={
        "revenue": "84 600 000", "expenses": "76 900 000", "ebit": "6 200 000", "amortization": "0",
        "revenue_trend": "FLAT", "employees": "24", "action": "next",
        "l_creditor": ["Банк А", "Лизинг", "Банк " + "Б" * 300], "l_balance": ["1 000 000", "500 000", "100 000"],
        "l_payment": ["50 000", "20 000", "5 000"], "l_maturity": ["завтра", "01.2020", "12.2027"]})
    assert r.status_code == 422
    assert "Обязательство №1: срок погашения — месяц и год через точку" in r.text
    assert "Обязательство №2: срок погашения уже прошёл" in r.text
    liabs = db.query(Liability).filter_by(application_id=app["id"]).order_by(Liability.position).all()
    assert len(liabs[2].creditor) == 120  # длиннее поля в форме не сохраним
    bad = {"creditor": "Банк А", "balance": 1, "maturity": "скоро"}
    assert anna.put(f"/api/applications/{app['id']}/financials", json={"liabilities": [bad]}).status_code == 422


def test_analyst_requests_listed_document_with_sane_deadline_and_finite_rate(anna, maria):
    app = draft(anna, SV, "WORKING", 5_000_000, 24)
    consent_all(anna, app["id"])
    assert submit_and_process(anna, app["id"])["status"] == "MANUAL_REVIEW"
    v = maria.get(f"/api/bank/applications/{app['id']}").json()["application"]["version"]
    r = maria.post(f"/api/bank/applications/{app['id']}/decision", json={
        "kind": "info", "expected_version": v, "comment": "Нужны документы", "doc_kind": "Справка от мамы"})
    assert r.status_code == 422 and r.json()["error"] == "doc_required"

    c = web_login("maria@bank.local")
    url = f"/bank/applications/{app['id']}/decision"
    r = c.post(url, data={"kind": "info", "version": v, "comment": "Нужны документы",
                          "doc_kind": "Другое", "due_days": "100000000"})
    assert "Срок ответа — от 1 до 30 дней" in r.text
    r = c.post(url, data={"kind": "counter", "version": v, "comment": "Меньше сумма", "amount": "3 000 000",
                          "term": "24", "rate": "NaN", "reasons": "DEBT_LOAD"})
    assert "Ставка должна быть от 0 до 100%" in r.text
    assert anna.get(f"/api/applications/{app['id']}").json()["status"] == "MANUAL_REVIEW"


def test_register_requires_name_in_letters():
    from app.main import app

    c = TestClient(app)

    def reg(name: str, email: str):
        return c.post("/api/auth/register", json={"email": email, "full_name": name, "password": "Gorizont2026",
                                                  "pd_consent": True})

    for bad in ("1", "Ольга", "<b>Ольга</b> Новикова", "Ольга 123"):
        r = reg(bad, "olga@new.test")
        assert r.status_code == 422 and "full_name" in r.json()["details"], bad
    assert reg("Петров И. О.", "petrov@new.test").status_code == 201
    assert reg("Анна-Мария Д’Артаньян", "anna-maria@new.test").status_code == 201
