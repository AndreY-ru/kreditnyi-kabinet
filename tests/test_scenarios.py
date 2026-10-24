"""Сквозные сценарии T1–T10 из раздела 9 исследования."""
from __future__ import annotations

from tests.helpers import (
    FNS_DOWN,
    OVERDUE,
    SV,
    YOUNG,
    consent_all,
    draft,
    process,
    statuses,
    submit_and_process,
)


def test_t1_stable_company_small_amount_auto_approved_to_disbursed(anna, igor):
    app = draft(anna, SV, "WORKING", 1_000_000, 12)
    consent_all(anna, app["id"])
    res = submit_and_process(anna, app["id"])
    assert res["status"] == "APPROVED_AUTO"
    assert res["offer"]["amount"] == 1_000_000
    assert res["offer"]["monthly_payment"] > 0

    assert anna.post(f"/api/applications/{app['id']}/offer/accept").json()["status"] == "SIGNING"
    # J5: финдиректор готовит и принимает, договор подписывает гендиректор с правом подписи
    r = anna.post(f"/api/applications/{app['id']}/sign")
    assert r.status_code == 403 and "Игорь Смирнов" in r.json()["message"]
    assert igor.post(f"/api/applications/{app['id']}/sign").json()["status"] == "DISBURSED"
    assert statuses(anna, app["id"]) == ["DRAFT", "SUBMITTED", "VALIDATING", "ENRICHING", "SCORING", "APPROVED_AUTO",
                                         "OFFER_ACCEPTED", "SIGNING", "DISBURSED"]


def test_t2_young_company_declined_with_reason_and_alternative(anna, db):
    from app.models import Decision

    fin = {"revenue": 3_000_000, "expenses": 2_500_000, "ebit": 500_000, "amortization": 0, "revenue_trend": "GROWTH",
           "liabilities": []}
    app = draft(anna, YOUNG, "WORKING", 600_000, 12, fin=fin)
    consent_all(anna, app["id"])
    res = submit_and_process(anna, app["id"])
    assert res["status"] == "DECLINED"
    assert [r["code"] for r in res["reasons"]] == ["BUSINESS_AGE"]
    assert "порог" not in res["reasons"][0]["text"]  # пороги клиенту не раскрываем
    d = db.query(Decision).filter_by(application_id=app["id"], outcome="DECLINED").one()
    # другой продукт, под который компания проходит по сроку работы, — не пустой список
    assert [a["name"] for a in d.conditions["alternatives"]] == ["Кредит на старт бизнеса"]


def test_t3_active_overdue_in_bki_declined(anna):
    fin = {"revenue": 18_000_000, "expenses": 16_900_000, "ebit": 1_500_000, "amortization": 200_000,
           "revenue_trend": "FLAT", "liabilities": []}
    app = draft(anna, OVERDUE, "WORKING", 1_000_000, 12, fin=fin)
    consent_all(anna, app["id"])
    res = submit_and_process(anna, app["id"])
    assert res["status"] == "DECLINED"
    assert "PAYMENT_HISTORY" in [r["code"] for r in res["reasons"]]


def test_t4_gray_zone_large_amount_counteroffer_accepted(anna, igor, maria):
    app = draft(anna, SV, "CREDIT_LINE", 12_000_000, 36, goal="equipment")
    consent_all(anna, app["id"])
    res = submit_and_process(anna, app["id"])
    assert res["status"] == "MANUAL_REVIEW"

    card = maria.get(f"/api/bank/applications/{app['id']}").json()
    assert card["scoring"]["zone"] == "GRAY"
    assert card["hint"]["amount"] <= 10_000_000
    assert maria.post(f"/api/bank/applications/{app['id']}/take").status_code == 204
    r = maria.post(f"/api/bank/applications/{app['id']}/decision", json={
        "kind": "counter", "expected_version": card["application"]["version"], "comment": "Снижаем до лимита без залога",
        "amount": 9_000_000, "term_months": 36, "rate": "20.9", "reasons": ["COLLATERAL", "DEBT_LOAD"]})
    assert r.status_code == 200, r.text
    client_view = anna.get(f"/api/applications/{app['id']}").json()
    assert client_view["status"] == "COUNTEROFFER"
    assert client_view["offer"]["amount"] == 9_000_000
    assert {r["code"] for r in client_view["reasons"]} == {"COLLATERAL", "DEBT_LOAD"}

    anna.post(f"/api/applications/{app['id']}/offer/accept")
    assert igor.post(f"/api/applications/{app['id']}/sign").json()["status"] == "DISBURSED"
    assert statuses(anna, app["id"])[-5:] == ["MANUAL_REVIEW", "COUNTEROFFER", "OFFER_ACCEPTED", "SIGNING", "DISBURSED"]


def test_t5_analyst_requests_info_client_answers_then_approved(anna, maria):
    app = draft(anna, SV, "WORKING", 5_000_000, 24)
    consent_all(anna, app["id"])
    assert submit_and_process(anna, app["id"])["status"] == "MANUAL_REVIEW"
    v = maria.get(f"/api/bank/applications/{app['id']}").json()["application"]["version"]
    r = maria.post(f"/api/bank/applications/{app['id']}/decision", json={
        "kind": "info", "expected_version": v, "comment": "Подтвердить рост выручки",
        "doc_kind": "Оборотно-сальдовая ведомость по счёту 62", "due_days": 7})
    assert r.status_code == 200, r.text
    view = anna.get(f"/api/applications/{app['id']}").json()
    assert view["status"] == "NEEDS_INFO" and view["info_request"]["doc_kind"].startswith("Оборотно")

    r = anna.post(f"/api/applications/{app['id']}/info-response", data={"comment": "Рост из-за нового контракта"},
                  files=[("files", ("osv_62.xlsx", b"synthetic-content", "application/vnd.ms-excel"))])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "MANUAL_REVIEW"

    v = maria.get(f"/api/bank/applications/{app['id']}").json()["application"]["version"]
    r = maria.post(f"/api/bank/applications/{app['id']}/decision", json={
        "kind": "approve", "expected_version": v, "comment": "Подтверждено", "rate": "19.5"})
    assert r.status_code == 200, r.text
    assert statuses(anna, app["id"])[-4:] == ["MANUAL_REVIEW", "NEEDS_INFO", "MANUAL_REVIEW", "APPROVED"]


def test_t6_offer_rejected_and_offer_expired(anna, db):
    from datetime import timedelta

    from app.models import Offer, utcnow
    from app.worker import expire_stale

    a1 = draft(anna, SV, "WORKING", 1_000_000, 12)
    consent_all(anna, a1["id"])
    submit_and_process(anna, a1["id"])
    assert anna.post(f"/api/applications/{a1['id']}/offer/reject").json()["status"] == "OFFER_REJECTED"

    a2 = draft(anna, SV, "WORKING", 1_200_000, 12)
    consent_all(anna, a2["id"])
    submit_and_process(anna, a2["id"])
    offer = db.query(Offer).filter_by(application_id=a2["id"]).one()
    offer.valid_until = utcnow() - timedelta(minutes=1)
    db.commit()
    assert expire_stale(db) == 1
    assert anna.get(f"/api/applications/{a2['id']}").json()["status"] == "EXPIRED"
    assert anna.post(f"/api/applications/{a2['id']}/offer/accept").status_code == 409


def test_t7_no_bki_consent_blocks_submit_and_no_request_to_bki(anna, db):
    from app.models import ExternalCheck, Job

    app = draft(anna, SV, "WORKING", 1_000_000, 12)
    anna.post(f"/api/applications/{app['id']}/consents", json={"kind": "PD"})
    r = anna.post(f"/api/applications/{app['id']}/submit")
    assert r.status_code == 422
    assert any(e["field"] == "consent_bki" for e in r.json()["details"])
    assert anna.get(f"/api/applications/{app['id']}").json()["status"] == "DRAFT"
    assert db.query(Job).count() == 0
    assert db.query(ExternalCheck).filter_by(source="BKI_MOCK").count() == 0


def test_t7b_revoked_consent_is_not_active(anna):
    app = draft(anna, SV, "WORKING", 1_000_000, 12)
    consent_all(anna, app["id"])
    assert anna.delete(f"/api/applications/{app['id']}/consents/BKI").status_code == 204
    assert anna.post(f"/api/applications/{app['id']}/submit").status_code == 422


def test_t8_fns_unavailable_retries_then_manual_review(anna, maria, db):
    from app.models import ExternalCheck

    fin = {"revenue": 12_000_000, "expenses": 10_800_000, "ebit": 1_100_000, "amortization": 100_000,
           "revenue_trend": "GROWTH", "liabilities": []}
    app = draft(anna, FNS_DOWN, "WORKING", 1_000_000, 12, fin=fin)
    consent_all(anna, app["id"])
    res = submit_and_process(anna, app["id"])
    assert res["status"] == "MANUAL_REVIEW"
    hist = statuses(anna, app["id"])
    assert hist.count("ENRICHING") == 3  # вход + 2 повтора
    fails = db.query(ExternalCheck).filter_by(application_id=app["id"], ok=False).count()
    assert fails == 3
    queue = maria.get("/api/bank/queue?tab=unassigned").json()
    item = next(q for q in queue if q["application_id"] == app["id"])
    assert item["score"] is None and "не ответил" in item["reason"]


def test_t9_double_submit_is_idempotent(anna, db):
    from app.models import ApplicationSnapshot, Job

    app = draft(anna, SV, "WORKING", 1_000_000, 12)
    consent_all(anna, app["id"])
    r1 = anna.post(f"/api/applications/{app['id']}/submit", headers={"Idempotency-Key": "k-1"})
    r2 = anna.post(f"/api/applications/{app['id']}/submit", headers={"Idempotency-Key": "k-1"})
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json()
    r3 = anna.post(f"/api/applications/{app['id']}/submit")  # без ключа — конфликт, а не дубль
    assert r3.status_code == 409
    assert db.query(Job).count() == 1
    assert db.query(ApplicationSnapshot).count() == 1


def test_t9_two_analysts_decide_concurrently_second_gets_conflict(anna, maria, petr):
    app = draft(anna, SV, "WORKING", 5_000_000, 24)
    consent_all(anna, app["id"])
    submit_and_process(anna, app["id"])
    v = maria.get(f"/api/bank/applications/{app['id']}").json()["application"]["version"]
    body = {"kind": "approve", "expected_version": v, "comment": "ok", "rate": "19.5"}
    r1 = maria.post(f"/api/bank/applications/{app['id']}/decision", json=body)
    r2 = petr.post(f"/api/bank/applications/{app['id']}/decision",
                   json={**body, "kind": "decline", "reasons": ["DEBT_LOAD"]})
    assert r1.status_code == 200
    assert r2.status_code == 409
    assert r2.json()["error"] == "version_conflict"
    assert anna.get(f"/api/applications/{app['id']}").json()["status"] == "APPROVED"


def test_t10_foreign_application_is_not_accessible(anna, elena):
    app = draft(anna, SV, "WORKING", 1_000_000, 12)
    assert elena.get(f"/api/applications/{app['id']}").status_code == 404
    assert elena.post(f"/api/applications/{app['id']}/withdraw").status_code == 404
    assert elena.get(f"/api/applications/{app['id']}/history").status_code == 404
    # и клиент не может вызывать API банка
    assert elena.get("/api/bank/queue").status_code == 403
    assert elena.post(f"/api/bank/applications/{app['id']}/decision",
                      json={"kind": "approve", "expected_version": 1, "comment": "x"}).status_code == 403


def test_process_runs_nothing_when_queue_empty():
    assert process() == 0
