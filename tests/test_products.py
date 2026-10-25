"""Каталог продуктов у администратора: создать, изменить, убрать в архив; версия карты скоринга через API."""
from __future__ import annotations

import copy

from tests.helpers import SV, company_id
from tests.test_web import web_login

NEW = {"code": "season_loan", "name": "Сезонный кредит", "kind": "LOAN", "description": "Под сезонные закупки",
       "goals": ["working", "cash_gap"], "min_amount": "300 000", "max_amount": "4 000 000", "min_term": "3",
       "max_term": "9", "min_business_months": "12", "unsecured_max": "", "base_rate": "17,5",
       "auto_limit": "1 000 000", "decision_days": "1", "requires_guarantor": "1", "active": "1"}


def _podbor_names(c, **params) -> list[str]:
    """Все продукты, которые подбор считает подходящими (а не только три лучших на экране)."""
    from app import db as dbm
    from app.models import Company
    from app.services.catalog import match_products

    q = {"goal": "working", "amount": 2_000_000, "term": 6, **params}
    with dbm.SessionLocal() as s:
        company = s.query(Company).filter_by(inn=SV).one()
        return [m.product.name for m in match_products(s, goal=q["goal"], amount=q["amount"], term=q["term"],
                                                       has_collateral=False, has_guarantor=True, company=company,
                                                       limit=50) if m.eligible]


def test_admin_creates_product_and_it_reaches_podbor(anna, db):
    from app.models import AuditLog, Product

    admin = web_login("admin@bank.local")
    assert "+ Добавить продукт" in admin.get("/bank/settings").text
    r = admin.post("/bank/settings/products/new", data=NEW)
    assert r.status_code == 200 and "Продукт «Сезонный кредит» добавлен" in r.text
    p = db.query(Product).filter_by(code="SEASON_LOAN").one()
    assert (p.version, p.base_rate, p.goals, p.max_amount) == (1, 17.5, ["working", "cash_gap"], 4_000_000)
    assert db.query(AuditLog).filter_by(action="product.create", entity_id=p.id).count() == 1
    assert "Сезонный кредит" in _podbor_names(anna)
    assert "Новый продукт" in admin.get("/bank/settings").text  # журнал изменений


def test_product_form_validates_and_keeps_what_was_typed(db):
    admin = web_login("admin@bank.local")
    r = admin.post("/bank/settings/products/new", data={**NEW, "code": "плохой код", "min_amount": "5 000 000",
                                                        "base_rate": "сто", "goals": []})
    assert r.status_code == 422
    for text in ("Код — латиница", "Максимальная сумма меньше минимальной", "Ставка — число от 0 до 100",
                 "Отметьте хотя бы одну цель"):
        assert text in r.text, text
    assert 'value="5 000 000"' in r.text.replace(" ", " ")  # введённое не потерялось
    assert admin.post("/bank/settings/products/new", data={**NEW, "code": "WORKING"}).status_code == 422  # код занят


def test_edit_bumps_version_and_archive_hides_product(anna, db):
    from app.models import Product

    admin = web_login("admin@bank.local")
    p = db.query(Product).filter_by(code="OVERDRAFT").one()
    page = admin.get(f"/bank/settings/products/{p.id}").text
    assert "Код не меняется" in page and "будет версия 2" in page
    form = {k: v for k, v in {
        "name": p.name, "kind": p.kind, "description": p.description, "min_amount": str(p.min_amount),
        "max_amount": str(p.max_amount), "min_term": str(p.min_term), "max_term": str(p.max_term),
        "min_business_months": str(p.min_business_months), "unsecured_max": "", "base_rate": "19,9",
        "auto_limit": str(p.auto_limit), "decision_days": str(p.decision_days), "active": "1"}.items()}
    form["goals"] = p.goals
    assert "Изменений нет" in admin.post(f"/bank/settings/products/{p.id}", data=form).text
    r = admin.post(f"/bank/settings/products/{p.id}", data={**form, "auto_limit": "1 500 000"})
    assert "сохранён, версия 2" in r.text and "auto_limit 2000000 → 1500000" in admin.get("/bank/settings").text
    assert "Овердрафт" in _podbor_names(anna, goal="cash_gap", amount=500_000)
    admin.post(f"/bank/settings/products/{p.id}", data={k: v for k, v in form.items() if k != "active"})
    db.expire_all()
    assert db.get(Product, p.id).active is False
    assert "Овердрафт" not in _podbor_names(anna, goal="cash_gap", amount=500_000)
    r = anna.post("/api/applications", json={"company_id": company_id(anna, SV), "product_id": p.id,
                                             "amount": 500_000, "term_months": 6, "goal": "cash_gap"})
    assert r.status_code == 422  # на продукте в архиве заявку не создать


def test_product_pages_are_admin_only(db):
    from app.models import Product

    pid = db.query(Product).first().id
    for email in ("maria@bank.local", "anna@demo.local"):
        c = web_login(email)
        assert c.get("/bank/settings/products/new").status_code == 403
        assert c.post(f"/bank/settings/products/{pid}", data=NEW).status_code == 403


def test_product_api(maria):
    from tests.conftest import api_client

    admin = api_client("admin@bank.local")
    body = {"code": "API_LOAN", "name": "Кредит через API", "kind": "LOAN", "goals": ["equipment"],
            "min_amount": 1_000_000, "max_amount": 9_000_000, "min_term": 12, "max_term": 48, "base_rate": "18.9"}
    assert maria.post("/api/admin/products", json=body).status_code == 403
    r = admin.post("/api/admin/products", json=body)
    assert r.status_code == 201 and r.json()["version"] == 1 and r.json()["active"] is True
    pid = r.json()["id"]
    r = admin.patch(f"/api/admin/products/{pid}", json={"auto_limit": 2_000_000})
    assert r.status_code == 200 and r.json()["auto_limit"] == 2_000_000 and r.json()["version"] == 2
    bad = admin.patch(f"/api/admin/products/{pid}", json={"auto_limit": 50_000_000})
    assert bad.status_code == 422 and bad.json()["details"]["auto_limit"].startswith("Авто-лимит не может")


def test_new_scoring_model_version_via_api_is_validated_before_activation():
    from app.services.scoring import DEFAULT_MODEL
    from tests.conftest import api_client

    admin = api_client("admin@bank.local")
    cfg = copy.deepcopy(DEFAULT_MODEL)
    cfg["zones"]["auto_from"] = 70
    r = admin.post("/api/admin/scoring-models", json={"version": "v1.1", "config": cfg})
    assert r.status_code == 201, r.text  # раньше — 500: в модели нет поля created_by
    broken = copy.deepcopy(DEFAULT_MODEL)
    broken["payment_discipline"] = {"clean": 10}  # старый формат
    r = admin.post("/api/admin/scoring-models", json={"version": "v1.2", "config": broken})
    assert r.status_code == 422 and "Конфигурация не работает" in r.text
    active = [m["version"] for m in admin.get("/api/admin/scoring-models").json() if m["active"]]
    assert active == ["v1.1"]


def test_precheck_and_podbor_survive_when_every_product_is_archived(anna, db):
    from app.models import Product

    db.query(Product).update({Product.active: False})
    db.commit()
    from tests.test_precheck import _pre

    body = _pre(anna, SV)
    assert body.status_code == 200 and body.json()["level"] == "low" and body.json()["products"] == []
    c = web_login("anna@demo.local")
    assert c.get("/check").status_code == 200 and c.get("/podbor").status_code == 200


def test_draft_on_archived_product_cannot_be_submitted(anna, db):
    from app.models import Product
    from tests.helpers import consent_all, draft

    app = draft(anna, SV, "WORKING", 1_000_000, 12)
    consent_all(anna, app["id"])
    db.query(Product).filter_by(code="WORKING").update({Product.active: False})
    db.commit()
    r = anna.post(f"/api/applications/{app['id']}/submit")
    assert r.status_code == 422 and any(e["field"] == "product_id" for e in r.json()["details"])
    c = web_login("anna@demo.local")
    page = c.get(f"/applications/{app['id']}/edit?check=1").text
    assert "больше не выдаётся" in page and f"/podbor?app_id={app['id']}" in page



def test_admin_can_roll_back_scoring_model_version():
    from app.services.scoring import DEFAULT_MODEL
    from tests.conftest import api_client

    admin = api_client("admin@bank.local")
    cfg = copy.deepcopy(DEFAULT_MODEL)
    cfg["zones"]["auto_from"] = 75
    assert admin.post("/api/admin/scoring-models", json={"version": "v1.5", "config": cfg}).status_code == 201
    web = web_login("admin@bank.local")
    page = web.get("/bank/settings").text
    assert "v1.5 · активна" in page and "v1.0 · включить" in page
    r = web.post("/bank/settings/models/v1.0/activate")
    assert "Активна версия карты v1.0" in r.text and "Включена версия карты" in r.text  # и в журнале
    active = [m["version"] for m in admin.get("/api/admin/scoring-models").json() if m["active"]]
    assert active == ["v1.0"]
    assert admin.post("/api/admin/scoring-models/v9.9/activate").status_code == 404
    assert web_login("maria@bank.local").post("/bank/settings/models/v1.5/activate").status_code == 403
