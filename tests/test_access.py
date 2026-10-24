"""Доступ к компаниям и право подписи: по чужому ИНН доступа нет, коллег добавляет руководитель."""
from __future__ import annotations

from tests.helpers import SV, company_id, consent_all, draft, process
from tests.test_web import web_login

NEW_CO = "7713550120"  # ООО «Новый горизонт»: в демо ни к кому не привязана


def test_cannot_join_foreign_company_by_inn(anna, elena):
    """Раньше любой клиент вводил чужой ИНН и получал заявки компании, мог принять и подписать кредит."""
    app = draft(anna, SV, "WORKING", 1_000_000, 12)
    r = elena.post("/api/companies", json={"inn": SV, "position": "кто угодно"})
    assert r.status_code == 403 and r.json()["error"] == "company_taken"
    assert elena.get(f"/api/applications/{app['id']}").status_code == 404
    assert SV not in [c["inn"] for c in elena.get("/api/companies").json()]


def test_first_member_of_new_company_is_admin_and_invites_colleague(anna, elena):
    r = anna.post("/api/companies", json={"inn": NEW_CO, "position": "Финансовый директор"})
    assert r.status_code == 201
    cid = r.json()["id"]
    me = next(m for m in anna.get(f"/api/companies/{cid}/members").json() if m["full_name"] == "Анна Петрова")
    assert me["can_sign"] is True

    r = anna.post(f"/api/companies/{cid}/members", json={"email": "elena@demo.local", "position": "Бухгалтер"})
    assert r.status_code == 201 and r.json()["can_sign"] is False
    assert NEW_CO in [c["inn"] for c in elena.get("/api/companies").json()]
    assert anna.post(f"/api/companies/{cid}/members", json={"email": "elena@demo.local"}).status_code == 422


def test_only_signer_can_invite(anna):
    cid = company_id(anna, SV)  # у Анны в «Северном ветре» нет права подписи
    r = anna.post(f"/api/companies/{cid}/members", json={"email": "elena@demo.local"})
    assert r.status_code == 403 and r.json()["error"] == "no_sign_right"


def test_inactive_product_cannot_be_used(anna, db):
    from app.models import Product

    p = db.query(Product).filter_by(code="WORKING").one()
    p.active = False
    db.commit()
    r = anna.post("/api/applications", json={"company_id": company_id(anna, SV), "product_id": p.id,
                                             "amount": 1_000_000, "term_months": 12, "goal": "working"})
    assert r.status_code == 422 and r.json()["error"] == "product_unavailable"


def test_signer_sees_banner_and_non_signer_has_no_sign_button(anna):
    app = draft(anna, SV, "WORKING", 1_000_000, 12)
    consent_all(anna, app["id"])
    anna.post(f"/api/applications/{app['id']}/submit")
    process()
    anna.post(f"/api/applications/{app['id']}/offer/accept")

    page = web_login("anna@demo.local").get(f"/applications/{app['id']}").text
    assert "Подписать (смоделировано)" not in page and "Игорь Смирнов" in page
    igor = web_login("igor@demo.local")
    assert "ждёт вашей подписи" in igor.get("/cabinet").text
    assert "Подписать (смоделировано)" in igor.get(f"/applications/{app['id']}").text
