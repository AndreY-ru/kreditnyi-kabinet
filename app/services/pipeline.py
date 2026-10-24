"""Асинхронный конвейер заявки: проверка → обогащение → скоринг → кредитное решение (этапы 6–8).

Запускается воркером по задаче из outbox. Каждый этап фиксируется отдельной транзакцией,
поэтому клиент видит продвижение заявки в реальном времени.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.domain.states import S
from app.domain.transitions import ActorRef, transition
from app.models import (
    Application,
    Decision,
    ExternalCheck,
    Offer,
    ReviewTask,
    ScoringModel,
    ScoringResult,
    utcnow,
)
from app.services import integrations
from app.services.catalog import alternatives_for
from app.services.companies import apply_egrul
from app.services.credit_logic import decide
from app.services.explain import enqueue_rephrase, explain
from app.services.scoring import DEFAULT_MODEL, ScoringInputs, score
from app.services.validation import active_consent, validate

log = logging.getLogger(__name__)
SYSTEM = ActorRef.system()


def active_model(db: Session) -> dict[str, Any]:
    row = db.scalar(select(ScoringModel).where(ScoringModel.active.is_(True)).order_by(ScoringModel.id.desc()))
    return row.config if row else DEFAULT_MODEL


def _pause() -> None:
    if settings.pipeline_step_delay > 0:
        time.sleep(settings.pipeline_step_delay)


def _record(db: Session, app: Application, res: integrations.CallResult, attempt: int) -> ExternalCheck:
    chk = ExternalCheck(application_id=app.id, source=res.source, endpoint=res.endpoint, request=res.request,
                        response=res.response, http_status=res.http_status, ok=res.ok, attempt=attempt,
                        duration_ms=res.duration_ms, simulated=True)
    db.add(chk)
    return chk


def process_application(db: Session, app_id: int) -> str:
    """Проводит заявку по конвейеру, насколько возможно без участия человека. Возвращает итоговый статус."""
    app = db.get(Application, app_id)
    if app is None:
        return "MISSING"

    if app.status == S.SUBMITTED:
        transition(db, app, S.VALIDATING, event="Автозапуск проверки", actor=SYSTEM)
        db.commit()
        _pause()

    if app.status == S.VALIDATING:
        errors = validate(db, app)
        if errors:
            app.validation_errors = errors
            transition(db, app, S.NEEDS_CORRECTION, event="Ошибки данных", actor=SYSTEM,
                       reason="; ".join(e["message"] for e in errors[:3]))
            db.commit()
            return app.status
        transition(db, app, S.ENRICHING, event="Валидация пройдена", actor=SYSTEM)
        db.commit()
        _pause()

    if app.status == S.ENRICHING:
        ok = _enrich(db, app)
        db.commit()
        if not ok:
            return app.status
        _pause()

    if app.status == S.SCORING:
        _score_and_decide(db, app)
        db.commit()
    return app.status


# --- Обогащение ---------------------------------------------------------------

def _enrich(db: Session, app: Application) -> bool:
    consent = active_consent(db, app, "BKI")
    if consent is None:  # без согласия запрос в БКИ технически невозможен
        _to_manual(db, app, "Нет действующего согласия на запрос в БКИ", flag="no_bki_consent")
        return False

    calls = {
        "egrul": lambda: integrations.fns_egrul(app.company.inn),
        "transparent": lambda: integrations.fns_transparent(app.company.inn),
        "bki": lambda: integrations.bki_report(app.company.inn, consent.id, f"Оценка заявки {app.number}"),
    }
    results: dict[str, dict[str, Any]] = {}
    max_attempts = max(1, settings.enrich_max_attempts)
    for attempt in range(1, max_attempts + 1):
        failed = []
        for name, call in calls.items():
            if name in results:
                continue
            res = call()
            _record(db, app, res, attempt)
            if res.ok and res.response is not None:
                results[name] = res.response
            else:
                failed.append(f"{res.source} ({res.http_status or 'нет ответа'})")
        db.flush()
        if not failed:
            break
        if attempt < max_attempts:
            transition(db, app, S.ENRICHING, event="Повтор запроса", actor=SYSTEM,
                       reason=f"Попытка {attempt} из {max_attempts}: не ответил {', '.join(failed)}")
            db.commit()
            time.sleep(settings.enrich_retry_delay * attempt)
        else:
            src = "ФНС" if any("FNS" in f for f in failed) else "БКИ"
            _to_manual(db, app, f"{src} не ответил после {max_attempts} попыток",
                       flag="source_unavailable", extra={"failed": failed})
            return False

    apply_egrul(app.company, results["egrul"])
    app.company.employees = results["transparent"].get("employees", app.company.employees)
    transition(db, app, S.SCORING, event="Проверки получены", actor=SYSTEM)
    return True


def _to_manual(db: Session, app: Application, reason: str, *, flag: str, extra: dict | None = None) -> None:
    app.flags = {**(app.flags or {}), flag: True, "manual_reason": reason, **(extra or {})}
    transition(db, app, S.MANUAL_REVIEW, event="Недоступность источника", actor=SYSTEM, reason=reason)
    db.add(Decision(application_id=app.id, outcome="MANUAL_REVIEW", source="SYSTEM", reason_codes=[],
                    review_reasons=[reason]))
    _open_task(db, app, reason, priority=1)


def _open_task(db: Session, app: Application, reason: str, priority: int = 0) -> ReviewTask:
    task = ReviewTask(application_id=app.id, reason=reason, priority=priority,
                      due_at=utcnow() + timedelta(hours=settings.review_sla_hours))
    db.add(task)
    return task


# --- Скоринг и решение ----------------------------------------------------------

def latest_checks(db: Session, app: Application) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    rows = db.scalars(select(ExternalCheck).where(ExternalCheck.application_id == app.id, ExternalCheck.ok.is_(True))
                      .order_by(ExternalCheck.id)).all()
    for r in rows:
        key = "bki" if r.source == "BKI_MOCK" else ("egrul" if "/egrul/" in r.endpoint else "transparent")
        out[key] = r.response or {}
    return out


def build_inputs(db: Session, app: Application) -> ScoringInputs:
    checks = latest_checks(db, app)
    f = app.financials
    bki = checks.get("bki", {})
    loans = bki.get("loans", [])
    declared_debt = sum(l.balance for l in app.liabilities)
    bki_debt = int(bki.get("total_balance") or 0)
    return ScoringInputs(
        amount=app.amount, term_months=app.term_months,
        business_months=app.company.business_months(),
        company_status=app.company.status,
        min_business_months=app.product.min_business_months,
        revenue=f.revenue if f else None, ebit=f.ebit if f else None,
        amortization=f.amortization if f else None, revenue_trend=f.revenue_trend if f else None,
        existing_debt=max(declared_debt, bki_debt),
        tax_arrears=int(checks.get("transparent", {}).get("tax_arrears") or 0),
        has_collateral=app.has_collateral, has_guarantor=app.has_guarantor, bki_loans=loans,
    )


def make_offer(db: Session, app: Application, decision: Decision, *, amount: int, term: int, rate,
               monthly: int) -> Offer:
    offer = Offer(application_id=app.id, decision_id=decision.id, amount=amount, term_months=term, rate=rate,
                  monthly_payment=monthly, valid_until=utcnow() + timedelta(days=settings.offer_valid_days))
    db.add(offer)
    return offer


def _score_and_decide(db: Session, app: Application) -> None:
    cfg = active_model(db)
    inp = build_inputs(db, app)
    out = score(inp, cfg)
    db.add(ScoringResult(application_id=app.id, model_version=out.model_version, score=out.score, base=out.base,
                         zone=out.zone, factors=out.factors, knockouts=out.knockouts,
                         inputs={**inp.as_dict(), "debt_load": out.debt_load, "ebitda": out.ebitda}))
    result = decide(app.product, inp, out, cfg)

    if result.status == "DECLINED":
        conditions: dict[str, Any] = {"knockouts": out.knockouts}
        if any(k["code"] == "BUSINESS_AGE" for k in out.knockouts):
            alts = alternatives_for(db, app.product, app.company, app.amount)
            conditions["alternatives"] = [{"product_id": p.id, "name": p.name} for p in alts]
        text, src = explain("DECLINED", result.reason_codes, app.product.name)
        db.add(Decision(application_id=app.id, outcome="DECLINED", source="SYSTEM", reason_codes=result.reason_codes,
                        conditions=conditions, explanation=text, explanation_source=src))
        reason = "; ".join(k["label"] for k in out.knockouts) or f"Оценка {out.score} ниже порога"
        transition(db, app, S.DECLINED, event="Решение системы", actor=SYSTEM, reason=reason)
        enqueue_rephrase(db, app.id, "DECLINED")
        return

    if result.status == "APPROVED_AUTO":
        o = result.offer or {}
        d = Decision(application_id=app.id, outcome="APPROVED_AUTO", source="SYSTEM", reason_codes=[],
                     conditions={k: str(v) for k, v in o.items()},
                     explanation=explain("APPROVED_AUTO", [], app.product.name)[0], explanation_source="TEMPLATE")
        db.add(d)
        db.flush()
        make_offer(db, app, d, amount=o["amount"], term=o["term_months"], rate=o["rate"], monthly=o["monthly_payment"])
        transition(db, app, S.APPROVED_AUTO, event="Решение системы", actor=SYSTEM,
                   reason=f"Оценка {out.score}, сумма в авто-лимите")
        return

    db.add(Decision(application_id=app.id, outcome="MANUAL_REVIEW", source="SYSTEM", reason_codes=result.reason_codes,
                    review_reasons=result.review_reasons, conditions={"hint": result.hint} if result.hint else None))
    transition(db, app, S.MANUAL_REVIEW, event="Серая зона", actor=SYSTEM, reason="; ".join(result.review_reasons))
    _open_task(db, app, "; ".join(result.review_reasons), priority=result.priority)
