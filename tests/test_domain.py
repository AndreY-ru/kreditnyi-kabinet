"""Модульные тесты: машина состояний, скоринг, кредитная логика, валидация, ИНН."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.states import TERMINAL, TRANSITIONS, Actor, S, can_transition
from app.services.companies import inn_is_valid
from app.services.credit_logic import annuity
from app.services.scoring import DEFAULT_MODEL, ScoringInputs, score
from tests.helpers import LIQUIDATING, SV, consent_all, draft, submit_and_process


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


def test_scorecard_matches_design_example():
    # Пример из макета карточки аналитика: 55 + 33 − 25 = 63
    out = score(_inputs())
    assert out.score == 63 and out.zone == "GRAY"
    pts = {f["code"]: f["points"] for f in out.factors}
    assert pts == {"business_age": 12, "payment_discipline": 10, "guarantor": 6, "revenue_trend": 5,
                   "request_to_revenue": -7, "debt_load": -18}


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
