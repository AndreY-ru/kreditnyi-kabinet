"""Смоук-тесты HTML-кабинетов: страницы рендерятся, CSRF и доступ работают."""
from __future__ import annotations

import re

from fastapi.testclient import TestClient

from tests.helpers import SV, consent_all, draft, submit_and_process


def web_login(email: str) -> TestClient:
    from app.main import app

    c = TestClient(app)
    page = c.get("/login")
    tok = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
    r = c.post("/login", data={"email": email, "password": "demo", "csrf_token": tok, "next": ""})
    assert r.status_code == 200
    # после входа сессия пересоздаётся, токен CSRF берём со страницы кабинета
    c.headers["X-CSRF-Token"] = re.search(r'name="csrf-token" content="([^"]+)"', r.text).group(1)
    return c


def test_client_pages_render(anna):
    app = draft(anna, SV, "WORKING", 5_000_000, 24)
    c = web_login("anna@demo.local")
    for path in ["/cabinet", "/podbor", "/company", f"/applications/{app['id']}/edit",
                 f"/applications/{app['id']}/consents", "/podbor/results?goal=working&amount=5000000&term=24"]:
        r = c.get(path)
        assert r.status_code == 200, path
        assert "Traceback" not in r.text


def test_bank_pages_render(anna):
    app = draft(anna, SV, "WORKING", 5_000_000, 24)
    consent_all(anna, app["id"])
    submit_and_process(anna, app["id"])
    c = web_login("maria@bank.local")
    for path in ["/bank/queue", "/bank/queue?tab=unassigned", "/bank/queue?tab=all", f"/bank/applications/{app['id']}",
                 "/bank/process", "/bank/settings"]:
        r = c.get(path)
        assert r.status_code == 200, path
    assert "Скоринг" in c.get(f"/bank/applications/{app['id']}").text


def test_post_without_csrf_rejected():
    from app.main import app

    c = TestClient(app)
    r = c.post("/login", data={"email": "anna@demo.local", "password": "demo"})
    assert r.status_code == 403


def test_client_cannot_open_bank_pages():
    c = web_login("anna@demo.local")
    assert c.get("/bank/queue").status_code == 403


def test_full_web_flow_from_podbor_to_status():
    c = web_login("anna@demo.local")
    r = c.get("/podbor")
    pid = re.search(r'name="product_id" value="(\d+)"', r.text).group(1)
    r = c.post("/applications/new", data={"product_id": pid, "goal": "working", "amount": "1 000 000", "term": "12",
                                          "guarantor": "1"})
    assert r.status_code == 200 and "Финансы компании" in r.text
    app_id = int(re.search(r"/applications/(\d+)/edit", str(r.url)).group(1))
    r = c.post(f"/applications/{app_id}/edit", data={
        "revenue": "84 600 000", "expenses": "76 900 000", "ebit": "6 200 000", "amortization": "300 000",
        "revenue_trend": "GROWTH", "employees": "24", "l_creditor": ["Банк А"], "l_balance": ["2 400 000"],
        "l_payment": ["160 000"], "l_maturity": ["08.2027"], "action": "next"})
    assert r.status_code == 200 and "Согласия и отправка" in r.text
    for kind in ("PD", "BKI", "BKI_GUARANTOR"):
        assert c.post(f"/applications/{app_id}/consents/{kind}", data={"value": "1"},
                      headers={"HX-Request": "true"}).status_code == 200
    r = c.post(f"/applications/{app_id}/submit", data={"idem": "web-1"})
    assert r.status_code == 200 and "KR-" in r.text
    from tests.helpers import process

    process()
    r = c.get(f"/applications/{app_id}")
    assert "Кредит одобрен" in r.text


def test_favicon_is_served():
    from app.main import app

    c = TestClient(app)
    r = c.get("/favicon.ico")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert 'rel="icon"' in c.get("/login").text


def test_draft_can_be_deleted_and_disappears_from_lists(anna):
    app = draft(anna, SV, "WORKING", 1_000_000, 12)
    c = web_login("anna@demo.local")
    assert "Удалить черновик" in c.get(f"/applications/{app['id']}/edit").text
    r = c.post(f"/applications/{app['id']}/withdraw")
    assert r.status_code == 200 and "Черновик удалён" in r.text
    for tab in ("active", "done"):
        page = c.get(f"/cabinet?tab={tab}").text
        assert f'/applications/{app["id"]}"' not in page and f'/applications/{app["id"]}/edit"' not in page
