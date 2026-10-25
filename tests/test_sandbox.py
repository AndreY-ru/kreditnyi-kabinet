"""Тестовые данные источников: правка кредитной истории и сведений ФНС меняет новые заявки, но не рассмотренные."""
from __future__ import annotations

import re

from fastapi.testclient import TestClient

from tests.helpers import SV, consent_all, draft, submit_and_process
from tests.test_web import web_login


def _sv_id(db) -> int:
    from app.models import Company

    return db.query(Company).filter_by(inn=SV).one().id


def test_only_admin_opens_sandbox(db):
    admin = web_login("admin@bank.local")
    page = admin.get("/bank/sandbox")
    assert page.status_code == 200 and "Тестовые данные источников" in page.text and SV in page.text
    assert 'href="/bank/sandbox"' in page.text  # пункт меню — только у администратора
    analyst = web_login("maria@bank.local")
    assert analyst.get("/bank/sandbox").status_code == 403
    assert 'href="/bank/sandbox"' not in analyst.get("/bank/queue").text
    assert web_login("anna@demo.local").post(f"/bank/sandbox/{SV}/bki", data={"action": "reset"}).status_code == 403


def test_mock_keeps_overrides_until_reset():
    from mocks.bki import app as bki

    c = TestClient(bki)
    assert c.get(f"/admin/reports/{SV}").json()["overridden"] is False
    loans = [{"creditor": "Банк Х", "balance": 1_000_000, "monthly_payment": 50_000,
              "overdues_24m": [{"days": 45, "amount": 100_000, "months_ago": 3}]}]
    assert c.put(f"/admin/reports/{SV}", json={"loans": loans}).json()["overridden"] is True
    report = c.get(f"/report/{SV}", params={"consent_id": "1", "purpose": "проверка"}).json()
    assert report["total_balance"] == 1_000_000 and report["loans"][0]["creditor"] == "Банк Х"
    assert c.put(f"/admin/reports/{SV}", json={"loans": [{"creditor": "", "balance": -1}]}).status_code == 422
    c.delete(f"/admin/reports/{SV}")
    report = c.get(f"/report/{SV}", params={"consent_id": "1", "purpose": "проверка"}).json()
    assert report["total_balance"] == 3_500_000  # исходные данные


def test_preset_affects_next_application_but_not_the_decided_one(anna, db):
    from app.models import Decision, ExternalCheck

    first = draft(anna, SV, "WORKING", 1_000_000, 12)
    consent_all(anna, first["id"])
    assert submit_and_process(anna, first["id"])["status"] == "APPROVED_AUTO"

    admin = web_login("admin@bank.local")
    r = admin.post(f"/bank/sandbox/{SV}/bki", data={"action": "preset:serious_recent"})
    assert r.status_code == 200 and "применён" in r.text
    assert "больше 30 дней за последний год" in r.text  # предпросмотр: без автоодобрения

    second = draft(anna, SV, "WORKING", 1_000_000, 12)
    consent_all(anna, second["id"])
    assert submit_and_process(anna, second["id"])["status"] == "MANUAL_REVIEW"
    reasons = db.query(Decision).filter_by(application_id=second["id"]).one().review_reasons
    assert any("просрочка больше 30 дней" in x for x in reasons)
    old = db.query(ExternalCheck).filter_by(application_id=first["id"], source="BKI_MOCK").one()
    assert all(not loan["overdues_24m"] for loan in old.response["loans"])  # отчёт рассмотренной заявки не менялся

    r = admin.post(f"/bank/sandbox/{SV}/bki", data={"action": "preset:current"})
    assert "отказ:" in r.text  # действующая просрочка — стоп-фактор
    admin.post(f"/bank/sandbox/{SV}/bki", data={"action": "reset"})
    third = draft(anna, SV, "WORKING", 1_000_000, 12)
    consent_all(anna, third["id"])
    assert submit_and_process(anna, third["id"])["status"] == "APPROVED_AUTO"


def test_form_saves_loans_and_history_and_rejects_bad_input(db):
    from mocks.bki import app as bki

    admin = web_login("admin@bank.local")
    url = f"/bank/sandbox/{SV}/bki"
    form = {"n": "2", "l0_creditor": "Банк А", "l0_kind": "кредит", "l0_balance": "2 000 000", "l0_monthly": "100 000",
            "l0_maturity": "08.2027", "l0_n": "2", "l0_o0_days": "20", "l0_o0_amount": "120 000", "l0_o0_ago": "2",
            "l1_creditor": "Лизинг Плюс", "l1_kind": "лизинг", "l1_balance": "500 000", "l1_monthly": "25 000",
            "l1_n": "1", "action": "save"}
    r = admin.post(url, data=form)
    assert r.status_code == 200 and "Кредитная история сохранена" in r.text
    loans = TestClient(bki).get(f"/admin/reports/{SV}").json()["loans"]
    assert [(l["creditor"], l["kind"], l["balance"]) for l in loans] == [("Банк А", "кредит", 2_000_000),
                                                                          ("Лизинг Плюс", "лизинг", 500_000)]
    assert loans[0]["overdues_24m"] == [{"days": 20, "amount": 120_000, "months_ago": 2}]
    assert "небольшие, недавно или часто" in admin.get(f"/bank/sandbox?inn={SV}").text

    bad = admin.post(url, data={**form, "l0_maturity": "скоро", "l0_o0_ago": "40"})
    assert bad.status_code == 422 and "погашение — в формате ММ.ГГГГ" in bad.text and "от 0 до 23" in bad.text
    assert TestClient(bki).get(f"/admin/reports/{SV}").json()["loans"][0]["maturity"] == "08.2027"  # не сохранено

    form_del = {**form, "l1_delete": "1", "l0_o0_delete": "1"}
    admin.post(url, data=form_del)
    loans = TestClient(bki).get(f"/admin/reports/{SV}").json()["loans"]
    assert len(loans) == 1 and loans[0]["overdues_24m"] == []


def test_fns_edit_updates_company_and_precheck(anna, db):
    from app.models import Company

    admin = web_login("admin@bank.local")
    cid = _sv_id(db)
    r = admin.post(f"/bank/sandbox/{SV}/fns", data={"status": "LIQUIDATING", "tax_arrears": "250 000"})
    assert r.status_code == 200 and "Сведения ФНС сохранены" in r.text
    db.expire_all()
    assert db.get(Company, cid).status == "LIQUIDATING"  # карточка компании обновилась сразу
    body = anna.post(f"/api/companies/{cid}/precheck", json={"amount": 1_000_000, "term_months": 12}).json()
    assert body["level"] == "low"
    admin.post(f"/bank/sandbox/{SV}/fns", data={"action": "reset"})
    db.expire_all()
    assert db.get(Company, cid).status == "ACTIVE"


def test_admin_adds_company_client_connects_it_and_it_can_be_removed_only_before(anna, db):
    admin = web_login("admin@bank.local")
    r = admin.post("/bank/sandbox/companies", data={"kind": "OOO", "name": "Ромашка", "registered_at": "2021-03-15",
                                                    "director": "Иванов Иван Иванович", "okved": "47.91",
                                                    "revenue_last_year": "40 000 000",
                                                    "expenses_last_year": "36 000 000"})
    assert r.status_code == 200 and "Компания ООО «Ромашка» создана" in r.text
    inn = re.search(r"ИНН (\d{10})\. Клиент может подключить", r.text).group(1)
    assert "добавлена вами" in r.text and "Не подключена" in r.text and "Удалить компанию" in r.text
    assert admin.post("/bank/sandbox/companies", data={"name": "Дубль", "inn": inn,
                                                       "registered_at": "2021-03-15"}).status_code == 422

    r = anna.post("/api/companies", json={"inn": inn})  # клиент подключает по ИНН — данные из «ЕГРЮЛ»
    assert r.status_code == 201 and r.json()["name"] == "ООО «Ромашка»" and r.json()["okved"] == "47.91"
    r = admin.post(f"/bank/sandbox/{inn}/remove")
    assert "уже подключена к кабинету" in r.text  # подключённую не удалить

    r = admin.post("/bank/sandbox/companies", data={"kind": "IP", "name": "Сидоров Пётр", "registered_at": "2024-01-10"})
    ip_inn = re.search(r"ИНН (\d{12})\. Клиент может подключить", r.text).group(1)
    assert "ИП Сидоров Пётр" in r.text
    r = admin.post(f"/bank/sandbox/{ip_inn}/remove")
    assert "удалена из источников" in r.text and ip_inn not in r.text.split("удалена из источников")[1]
    assert admin.post(f"/bank/sandbox/{SV}/remove").status_code == 200  # справочную — только сообщение
    assert admin.post("/bank/sandbox/123/bki", data={"action": "reset"}).status_code == 404  # неверный ИНН


def test_new_company_form_reports_problems():
    admin = web_login("admin@bank.local")
    r = admin.post("/bank/sandbox/companies", data={"name": "", "inn": "12345", "registered_at": "вчера"})
    assert r.status_code == 422
    for text in ("ИНН — 10 цифр", "Укажите название", "Дата регистрации"):
        assert text in r.text, text
    r = admin.post("/bank/sandbox/companies", data={"name": "Неверный", "inn": "7701234567", "registered_at": "2020-01-01"})
    assert r.status_code == 422 and "контрольная сумма" in r.text
