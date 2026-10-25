"""Регистрация и профиль: новый клиент проходит путь сам, без заранее заведённых демо-пользователей."""
from __future__ import annotations

import re

from fastapi.testclient import TestClient

from tests.helpers import GOOD_FIN, process

NEW_CO = "7713550120"  # ООО «Новый горизонт» — ни к кому не привязана


def browser() -> tuple[TestClient, str]:
    from app.main import app

    c = TestClient(app)
    tok = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/register").text).group(1)
    return c, tok


def form(**kw):
    return {"full_name": "Ольга Новикова", "email": "olga@new.test", "password": "Gorizont2026",
            "password2": "Gorizont2026", "pd_consent": "1", "next": "", **kw}


def test_new_client_registers_adds_company_and_gets_loan():
    c, tok = browser()
    assert "Зарегистрироваться" in c.get("/login").text
    r = c.post("/register", data={**form(), "csrf_token": tok})
    assert r.status_code == 200 and str(r.url).endswith("/company") and "Аккаунт создан" in r.text
    c.headers["X-CSRF-Token"] = re.search(r'name="csrf-token" content="([^"]+)"', r.text).group(1)
    r = c.post("/company/add", data={"inn": NEW_CO, "position": "Генеральный директор"})
    assert r.status_code == 200 and "Новый горизонт" in r.text

    co = c.get("/api/companies").json()[0]
    pid = next(p["id"] for p in c.get("/api/products").json() if p["code"] == "WORKING")
    app = c.post("/api/applications", json={"company_id": co["id"], "product_id": pid, "amount": 1_000_000,
                                            "term_months": 12, "goal": "working"}).json()
    fin = {**GOOD_FIN, "revenue": 52_000_000, "expenses": 46_800_000, "ebit": 5_000_000,
           "liabilities": [{"creditor": "Банк Ж", "balance": 1_200_000, "monthly_payment": 70_000}]}
    assert c.put(f"/api/applications/{app['id']}/financials", json=fin).status_code == 200
    for kind in ("PD", "BKI", "BKI_GUARANTOR"):
        c.post(f"/api/applications/{app['id']}/consents", json={"kind": kind})
    assert c.post(f"/api/applications/{app['id']}/submit").status_code == 200
    process()
    assert c.get(f"/api/applications/{app['id']}").json()["status"] == "APPROVED_AUTO"
    c.post(f"/api/applications/{app['id']}/offer/accept")
    assert c.post(f"/api/applications/{app['id']}/sign").json()["status"] == "DISBURSED"  # первый — с правом подписи


def test_registration_validates_input():
    c, tok = browser()
    cases = [
        (form(password="12345678", password2="12345678"), "Слишком простой"),
        (form(password="abc", password2="abc"), "не короче 8"),
        (form(password2="other-pass-1"), "Пароли не совпадают"),
        (form(pd_consent=""), "Без согласия"),
        (form(email="anna@demo.local"), "уже зарегистрирован"),
        (form(email="not-an-email"), "Проверьте email"),
    ]
    for data, message in cases:
        r = c.post("/register", data={**data, "csrf_token": tok})
        assert r.status_code == 422 and message in r.text, message
    assert c.get("/api/auth/me").status_code == 401  # ни одна попытка не залогинила


def test_registration_api_and_client_role_only():
    from app.main import app

    c = TestClient(app)
    r = c.post("/api/auth/register", json={"email": "Petr@New.Test", "full_name": "Пётр Новый",
                                           "password": "Gorizont2026", "pd_consent": True})
    assert r.status_code == 201 and r.json()["role"] == "CLIENT" and r.json()["email"] == "petr@new.test"
    assert c.get("/bank/queue").status_code == 403  # регистрация не даёт доступ к кабинету банка


def test_mass_registration_is_limited():
    from app.main import app

    c = TestClient(app)
    codes = [c.post("/api/auth/register", json={"email": f"bot{i}@x.test", "full_name": "Бот Ботов",
                                                "password": "Gorizont2026", "pd_consent": True}).status_code
             for i in range(11)]
    assert codes[:10] == [201] * 10 and codes[10] == 429


def test_change_password():
    from app.main import app
    from tests.test_web import web_login

    c = web_login("igor@demo.local")
    assert "Сменить пароль" in c.get("/account").text
    r = c.post("/account/password", data={"current": "wrong", "new": "NewPass2026", "new2": "NewPass2026"})
    assert r.status_code == 422 and "Текущий пароль указан неверно" in r.text
    r = c.post("/account/password", data={"current": "demo", "new": "NewPass2026", "new2": "NewPass2026"})
    assert r.status_code == 200 and "Пароль изменён" in r.text
    fresh = TestClient(app)
    assert fresh.post("/api/auth/login", json={"email": "igor@demo.local", "password": "demo"}).status_code == 401
    assert fresh.post("/api/auth/login", json={"email": "igor@demo.local", "password": "NewPass2026"}).status_code == 200
    assert "Профиль" in web_login("maria@bank.local").get("/account").text  # и у сотрудника банка
