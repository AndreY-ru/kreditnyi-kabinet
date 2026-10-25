"""Модульные тесты: машина состояний, скоринг, кредитная логика, валидация, ИНН."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.states import TERMINAL, TRANSITIONS, Actor, S, can_transition
from app.services.companies import inn_is_valid
from app.services.credit_logic import annuity, decide
from app.services.scoring import DEFAULT_MODEL, ScoringInputs, score
from tests.helpers import (
    GOOD_FIN,
    LIQUIDATING,
    SV,
    company_id,
    consent_all,
    draft,
    product_id,
    submit_and_process,
)


def test_terminal_states_have_no_exits():
    for st in TERMINAL:
        assert st not in TRANSITIONS


def test_actor_rights_enforced():
    assert can_transition(S.DRAFT, S.SUBMITTED, Actor.CLIENT)
    assert not can_transition(S.DRAFT, S.SUBMITTED, Actor.ANALYST)
    assert not can_transition(S.MANUAL_REVIEW, S.APPROVED, Actor.CLIENT)
    assert not can_transition(S.SCORING, S.APPROVED, Actor.SYSTEM)  # APPROVED только через аналитика
    assert not can_transition(S.DECLINED, S.APPROVED, Actor.ANALYST)


def test_invalid_transition_rejected_by_service(anna, db):
    from app.domain.transitions import ActorRef, InvalidTransition, transition
    from app.models import Application

    app = draft(anna, SV, "WORKING", 1_000_000, 12)
    row = db.get(Application, app["id"])
    with pytest.raises(InvalidTransition):
        transition(db, row, S.APPROVED, event="hack", actor=ActorRef.system())


def _inputs(**kw) -> ScoringInputs:
    base = dict(amount=12_000_000, term_months=36, business_months=91, company_status="ACTIVE",
                min_business_months=12, revenue=84_600_000, ebit=6_200_000, amortization=300_000,
                revenue_trend="GROWTH", existing_debt=3_500_000, tax_arrears=0, has_collateral=False,
                has_guarantor=True, bki_loans=[])
    base.update(kw)
    return ScoringInputs(**base)


def test_scorecard_matches_card_example():
    # Карточка KR-0002 («Северный ветер», линия 12 млн): 64 + 12 − 24 = 52, вероятность дефолта ≈ 8,4%
    out = score(_inputs())
    assert out.score == 52 and out.zone == "GRAY" and abs(out.pd - 0.084) < 0.001
    pts = {f["code"]: f["points"] for f in out.factors}
    assert pts == {"business_age": 5, "payment_discipline": 2, "guarantor": 3, "revenue_trend": 2,
                   "request_to_revenue": -2, "debt_load": -22}


def test_scoring_is_deterministic_and_bounded():
    a, b = score(_inputs()), score(_inputs())
    assert a.score == b.score and a.factors == b.factors
    worst = score(_inputs(business_months=2, revenue=1, ebit=-5, revenue_trend="DECLINE", tax_arrears=10**6,
                          has_guarantor=False))
    assert 0 <= worst.score <= 100 and worst.zone == "LOW"


def test_knockouts():
    out = score(_inputs(company_status="LIQUIDATING"))
    assert [k["code"] for k in out.knockouts] == ["COMPANY_STATUS"]
    loans = [{"creditor": "Б", "current_overdue_days": 45, "current_overdue_amount": 380_000, "overdues_24m": []}]
    assert [k["code"] for k in score(_inputs(bki_loans=loans)).knockouts] == ["ACTIVE_OVERDUE"]


def _product(code: str):
    from app.models import Product
    from app.seed import PRODUCTS

    return Product(**next(p for p in PRODUCTS if p["code"] == code))


def test_payment_coverage_depends_on_term():
    """Долговая нагрузка не видит срока, покрытие платежей — видит: 2 млн на 6 мес. и на 36 мес. — разные решения."""
    working = _product("WORKING")
    short = _inputs(amount=2_000_000, term_months=6, existing_monthly=218_000)
    res = decide(working, short, score(short), DEFAULT_MODEL)
    assert res.status == "MANUAL_REVIEW" and res.metrics["dscr"] < 1
    assert any(r.startswith("Платежи по всем кредитам") for r in res.review_reasons)
    assert res.hint["amount"] < 2_000_000 and "или вся сумма на" in res.hint["text"]
    long = _inputs(amount=2_000_000, term_months=36, existing_monthly=218_000)
    res = decide(working, long, score(long), DEFAULT_MODEL)
    assert res.status == "APPROVED_AUTO" and res.metrics["dscr"] >= 1.25


def test_revenue_far_above_fns_goes_to_analyst():
    working = _product("WORKING")
    inflated = _inputs(amount=1_000_000, term_months=24, revenue=150_000_000, fns_revenue=84_600_000)
    res = decide(working, inflated, score(inflated), DEFAULT_MODEL)
    assert res.status == "MANUAL_REVIEW" and "FINANCIALS" in res.reason_codes
    assert any("на 77% выше, чем в отчётности ФНС" in r for r in res.review_reasons)
    grown = _inputs(amount=1_000_000, term_months=24, revenue=95_000_000, fns_revenue=84_600_000)  # +12% — рост
    assert decide(working, grown, score(grown), DEFAULT_MODEL).status == "APPROVED_AUTO"
    young = _inputs(amount=1_000_000, term_months=24, business_months=8, min_business_months=0, fns_revenue=0)
    assert not any("ФНС" in r for r in decide(working, young, score(young), DEFAULT_MODEL).review_reasons)


def test_pipeline_takes_payments_from_application_and_revenue_from_fns(anna, db):
    from app.models import Decision, ScoringResult

    app = draft(anna, SV, "WORKING", 3_000_000, 6, fin={**GOOD_FIN, "revenue": 150_000_000, "expenses": 140_000_000})
    consent_all(anna, app["id"])
    assert submit_and_process(anna, app["id"])["status"] == "MANUAL_REVIEW"
    sc = db.query(ScoringResult).filter_by(application_id=app["id"]).one()
    assert sc.model_version == "v1.0" and sc.inputs["existing_monthly"] == 218_000
    assert sc.inputs["fns_revenue"] == 84_600_000 and sc.inputs["dscr"] < 1.25
    reasons = db.query(Decision).filter_by(application_id=app["id"]).one().review_reasons
    assert any("отчётности ФНС" in r for r in reasons) and any("Платежи по всем кредитам" in r for r in reasons)


def test_annuity():
    assert annuity(1_200_000, Decimal("0"), 12) == 100_000
    p = annuity(1_000_000, Decimal("18.5"), 12)
    assert 91_000 < p < 93_000


def test_inn_checksum():
    assert inn_is_valid("7701234560") and inn_is_valid("772012345670")
    assert not inn_is_valid("7701234567") and not inn_is_valid("12345")


def test_liquidating_company_declined(anna):
    fin = {"revenue": 3_000_000, "expenses": 3_900_000, "ebit": 100_000, "amortization": 0,
           "revenue_trend": "DECLINE", "liabilities": []}
    app = draft(anna, LIQUIDATING, "OVERDRAFT", 300_000, 6, guarantor=False, fin=fin)
    consent_all(anna, app["id"], guarantor=False)
    res = submit_and_process(anna, app["id"])
    assert res["status"] == "DECLINED"
    assert [r["code"] for r in res["reasons"]] == ["COMPANY_STATUS"]


def test_validation_lists_missing_fields(anna):
    fin = {"revenue": 10_000_000, "expenses": 9_000_000, "ebit": 1_000_000, "revenue_trend": "FLAT", "liabilities": []}
    app = draft(anna, SV, "WORKING", 1_000_000, 12, fin=fin)  # амортизация не указана
    consent_all(anna, app["id"])
    r = anna.post(f"/api/applications/{app['id']}/submit")
    assert r.status_code == 422
    assert [e["field"] for e in r.json()["details"]] == ["amortization"]


def test_amount_outside_product_limits_is_validation_error(anna):
    app = draft(anna, SV, "WORKING", 50_000_000, 12)
    consent_all(anna, app["id"])
    r = anna.post(f"/api/applications/{app['id']}/submit")
    assert r.status_code == 422 and r.json()["details"][0]["field"] == "amount"


def test_scoring_model_version_saved_with_result(anna, db):
    from app.models import ScoringResult

    app = draft(anna, SV, "WORKING", 1_000_000, 12)
    consent_all(anna, app["id"])
    submit_and_process(anna, app["id"])
    assert db.query(ScoringResult).one().model_version == DEFAULT_MODEL["version"]


def test_snapshot_is_immutable_record_with_hash(anna, db):
    from app.models import ApplicationSnapshot

    app = draft(anna, SV, "WORKING", 1_000_000, 12)
    consent_all(anna, app["id"])
    submit_and_process(anna, app["id"])
    snap = db.query(ApplicationSnapshot).one()
    assert len(snap.sha256) == 64 and snap.data["amount"] == 1_000_000
    r = anna.put(f"/api/applications/{app['id']}/financials", json={"revenue": 1})
    assert r.status_code == 409  # после отправки данные не меняются


def test_new_draft_takes_financials_from_last_complete_application(anna):
    """Незаполненный черновик не должен «затирать» подстановку: берём последнюю заявку с прибылью."""
    draft(anna, SV, "WORKING", 1_000_000, 12)  # полные финансы и 2 обязательства
    body = {"company_id": company_id(anna, SV), "product_id": product_id(anna, "OVERDRAFT"), "amount": 500_000,
            "term_months": 6, "goal": "cash_gap"}
    incomplete = anna.post("/api/applications", json=body).json()["id"]
    anna.put(f"/api/applications/{incomplete}/financials", json={"ebit": None, "liabilities": []})

    newest = anna.post("/api/applications", json=body).json()["id"]
    r = anna.put(f"/api/applications/{newest}/financials", json={})  # ничего не меняем, смотрим подставленное
    assert r.status_code == 200
    from app import db as dbm
    from app.models import Application

    with dbm.SessionLocal() as s:
        app = s.get(Application, newest)
        assert app.financials.ebit == 6_200_000 and app.financials.revenue_trend == "GROWTH"
        assert len(app.liabilities) == 2


def test_profit_far_above_fns_goes_to_analyst():
    """Завысить прибыль выгоднее всего: она в долговой нагрузке и покрытии. Сравниваем рентабельность с ФНС."""
    working = _product("WORKING")
    fns = dict(revenue=84_600_000, fns_revenue=84_600_000, fns_expenses=76_900_000)  # по ФНС 9,1% выручки
    inflated = _inputs(amount=1_000_000, term_months=24, ebit=15_000_000, amortization=300_000, **fns)  # 18,1%
    res = decide(working, inflated, score(inflated), DEFAULT_MODEL)
    assert res.status == "MANUAL_REVIEW" and "FINANCIALS" in res.reason_codes
    assert any("Прибыль в анкете — 18,1% выручки, по отчётности ФНС — 9,1%" in r for r in res.review_reasons)
    honest = _inputs(amount=1_000_000, term_months=24, ebit=10_000_000, amortization=1_500_000, **fns)  # 13,6%
    assert not any("ФНС" in r for r in decide(working, honest, score(honest), DEFAULT_MODEL).review_reasons)
    loss = _inputs(amount=1_000_000, term_months=24, revenue=40_000_000, fns_revenue=40_000_000,
                   fns_expenses=42_000_000, ebit=4_000_000, amortization=0)
    assert any("убыток" in r for r in decide(working, loss, score(loss), DEFAULT_MODEL).review_reasons)
    young = _inputs(amount=1_000_000, term_months=24, business_months=8, min_business_months=0, ebit=15_000_000, **fns)
    assert not any("ФНС" in r for r in decide(working, young, score(young), DEFAULT_MODEL).review_reasons)


def test_pipeline_compares_declared_profit_with_fns(anna, db):
    from app.models import Decision

    app = draft(anna, SV, "WORKING", 1_000_000, 12, fin={**GOOD_FIN, "ebit": 15_000_000})
    consent_all(anna, app["id"])
    assert submit_and_process(anna, app["id"])["status"] == "MANUAL_REVIEW"
    reasons = db.query(Decision).filter_by(application_id=app["id"]).one().review_reasons
    assert any(r.startswith("Прибыль в анкете") and "по отчётности ФНС — 9,1%" in r for r in reasons)
