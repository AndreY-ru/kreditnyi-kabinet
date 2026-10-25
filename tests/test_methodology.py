"""Методика v1.0: калибровка карты, тяжесть и размер просрочек, залог через LGD, ставка = база + PD × LGD."""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

from app.services.credit_logic import decide, lgd_of, limits, rate_for
from app.services.scoring import CALIBRATED, DEFAULT_MODEL, classify_overdues, pd_of, score, score_of
from tests.helpers import GOOD_FIN, SV, consent_all, draft, submit_and_process
from tests.test_domain import _inputs, _product

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import calibrate  # noqa: E402


# --- Калибровка ---------------------------------------------------------------------------------------

def test_shipped_scorecard_is_what_the_script_produces():
    """scorecard.json не правят руками: тот же портфель и seed дают те же баллы и пороги."""
    data = calibrate.generate(200_000, 2026)
    card = calibrate.fit_card(data[: len(data) * 2 // 3])
    assert json.loads(json.dumps(card)) == CALIBRATED


def test_calibrated_card_is_monotone_discriminates_and_matches_observed_pd():
    data = calibrate.generate(30_000, 7)  # другой портфель, меньше — проверяем свойства, а не числа
    card = calibrate.fit_card(data[:20_000])
    assert calibrate.monotonic_problems(card) == []
    res = calibrate.evaluate(card, data[20_000:])
    assert res["gini"] > 0.5
    for band in res["bands"]:  # прогноз PD по шкале и фактическая доля дефолтов близки
        if band["n"] > 500:
            assert abs(band["predicted"] - band["observed"]) < 0.03 + 0.15 * band["observed"], band
    assert res["dscr"]["ниже 1,25"]["dr"] > 2 * res["dscr"]["1,25 и выше"]["dr"]  # норма покрытия оправдана


def test_scale_and_thresholds_follow_risk_appetite():
    assert abs(pd_of(60) - 0.05) < 1e-9 and abs(pd_of(70) / (1 - pd_of(70)) * 2 - pd_of(60) / (1 - pd_of(60))) < 1e-9
    assert abs(score_of(pd_of(57.3)) - 57.3) < 1e-9
    z = DEFAULT_MODEL["zones"]
    assert pd_of(z["auto_from"]) <= 0.03 < pd_of(z["auto_from"] - 1)  # автоодобрение — PD не выше 3%
    lgd = DEFAULT_MODEL["credit"]["lgd_unsecured"]
    assert pd_of(z["decline_below"]) * lgd <= 0.05 < pd_of(z["decline_below"] - 1) * lgd  # потери ≤ 5 п. п.


# --- Платёжная дисциплина: срок и размер ---------------------------------------------------------------

def _history(*episodes, balance=2_000_000):
    return [{"balance": balance, "overdues_24m": [{"days": d, "amount": a, "months_ago": m} for d, a, m in episodes]}]


def test_overdue_severity_by_days_amount_and_recency():
    cfg = DEFAULT_MODEL
    level = lambda *e, **kw: classify_overdues(_history(*e, **kw), cfg)[0]  # noqa: E731
    assert level((4, 300_000, 2)) == "clean"  # до 5 дней — техническая (590-П: хорошее обслуживание)
    assert level((20, 30_000, 2)) == "clean"  # 30 тыс. ₽ — ниже порога существенности
    assert level((20, 60_000, 2), balance=10_000_000) == "clean"  # 60 тыс. ₽ — меньше 1% долга в 10 млн
    assert level((20, 60_000, 15)) == "minor_old"
    assert level((20, 60_000, 3)) == "minor_recent"
    assert level((10, 60_000, 15), (12, 70_000, 18), (8, 80_000, 20)) == "minor_recent"  # часто
    assert level((45, 60_000, 15)) == "serious"
    assert level((120, 60_000, 20)) == "default"
    pts = DEFAULT_MODEL["payment_discipline"]["points"]
    assert pts["clean"] > pts["minor_old"] >= pts["minor_recent"] > pts["serious"] > pts["default"]


def test_small_overdue_does_not_lower_score_but_large_one_does():
    clean = score(_inputs()).score
    assert score(_inputs(bki_loans=_history((15, 20_000, 3)))).score == clean
    assert score(_inputs(bki_loans=_history((15, 400_000, 3)))).score < clean


# --- Залог: LGD, ставка, лимит -------------------------------------------------------------------------

def test_collateral_lowers_lgd_and_rate_but_not_score():
    bare = _inputs(amount=10_000_000)
    estate = _inputs(amount=10_000_000, has_collateral=True, collateral_kind="REAL_ESTATE", collateral_value=10_000_000)
    assert score(estate).score == score(bare).score  # залог — не про вероятность дефолта
    assert lgd_of(bare, DEFAULT_MODEL) == 0.40
    # 10 млн недвижимости с дисконтом 40% = 6 млн покрытия: 0,4 × 4/10 + 0,2 × 6/10 = 0,28
    assert abs(lgd_of(estate, DEFAULT_MODEL) - 0.28) < 1e-9
    line, s = _product("CREDIT_LINE"), score(bare).score
    assert rate_for(line, s, DEFAULT_MODEL, 0.28) < rate_for(line, s, DEFAULT_MODEL, 0.40)
    deposit = _inputs(amount=10_000_000, has_collateral=True, collateral_kind="DEPOSIT", collateral_value=10_000_000)
    assert lgd_of(deposit, DEFAULT_MODEL) == 0 and rate_for(line, s, DEFAULT_MODEL, 0) == Decimal("17.90")


def test_collateral_raises_unsecured_limit_by_discounted_value():
    line = _product("CREDIT_LINE")  # без залога — до 10 млн
    big = _inputs(amount=12_000_000, existing_debt=0, ebit=20_000_000)
    assert limits(big, score(big), line, DEFAULT_MODEL)["unsecured"] == 10_000_000
    res = decide(line, big, score(big), DEFAULT_MODEL)
    assert any(r.startswith("Без залога доступно не больше 10 млн ₽") for r in res.review_reasons)
    car = _inputs(amount=12_000_000, existing_debt=0, ebit=20_000_000, has_collateral=True, collateral_kind="VEHICLE",
                  collateral_value=5_000_000)  # 5 млн × 60% = 3 млн сверх лимита
    assert limits(car, score(car), line, DEFAULT_MODEL)["unsecured"] == 13_000_000
    assert not any("Без залога" in r for r in decide(line, car, score(car), DEFAULT_MODEL).review_reasons)


def test_collateral_details_required_and_used_by_pipeline(anna, db):
    from app.models import Decision, Offer, ScoringResult

    app = draft(anna, SV, "WORKING", 1_000_000, 12, collateral=True, fin=GOOD_FIN)
    consent_all(anna, app["id"])
    r = anna.post(f"/api/applications/{app['id']}/submit")
    assert r.status_code == 422 and {"collateral_kind", "collateral_value"} <= {e["field"] for e in r.json()["details"]}
    r = anna.patch(f"/api/applications/{app['id']}", json={"collateral_kind": "REAL_ESTATE",
                                                          "collateral_value": 4_000_000})
    assert r.status_code == 200 and r.json()["collateral_kind"] == "REAL_ESTATE"
    assert submit_and_process(anna, app["id"])["status"] == "APPROVED_AUTO"
    sc = db.query(ScoringResult).filter_by(application_id=app["id"]).one()
    assert sc.inputs["lgd"] == 0.2  # 4 млн × 60% = 2,4 млн покрывают весь 1 млн
    offer = db.query(Offer).filter_by(application_id=app["id"]).one()
    unsecured = rate_for(_product("WORKING"), sc.score, DEFAULT_MODEL)
    assert offer.rate < unsecured and db.query(Decision).filter_by(application_id=app["id"]).count() == 1
    bad = anna.patch(f"/api/applications/{app['id']}", json={"collateral_kind": "YACHT"})
    assert bad.status_code in (409, 422)


# --- Проверка шансов и версии карты ----------------------------------------------------------------------

def test_precheck_distinguishes_minor_and_major_overdues(anna):
    from tests.test_precheck import _pre

    minor, major = (_pre(anna, SV, overdues=o).json() for o in ("minor", "major"))
    tone = lambda b: next(f["tone"] for f in b["factors"] if f["title"] == "Кредитная история")  # noqa: E731
    assert tone(minor) == "warn" and tone(major) == "bad"


def test_new_model_version_becomes_active_on_start(db):
    from app.models import ScoringModel
    from app.seed import seed_reference

    old = db.query(ScoringModel).one()
    old.version, old.config = "v0.4", {"version": "v0.4"}
    db.commit()
    seed_reference(db)
    active = db.query(ScoringModel).filter_by(active=True).all()
    assert [m.version for m in active] == ["v1.0"] and db.query(ScoringModel).count() == 2


def test_serious_recent_or_default_history_blocks_auto_approval_even_with_high_score():
    working = _product("WORKING")
    base = dict(amount=1_000_000, term_months=24, existing_monthly=218_000)
    clean = _inputs(**base)
    assert decide(working, clean, score(clean), DEFAULT_MODEL).status == "APPROVED_AUTO"
    for history, text in (((45, 300_000, 3), "больше 30 дней за последний год"),
                          ((120, 300_000, 20), "больше 90 дней за 2 года")):
        inp = _inputs(**base, bki_loans=_history(history))
        res = decide(working, inp, score(inp), DEFAULT_MODEL)
        assert res.status == "MANUAL_REVIEW" and "PAYMENT_HISTORY" in res.reason_codes
        assert any(text in r for r in res.review_reasons)
    old = _inputs(**base, bki_loans=_history((45, 300_000, 15)))  # 45 дней, но больше года назад — только баллы
    assert decide(working, old, score(old), DEFAULT_MODEL).status == "APPROVED_AUTO"
