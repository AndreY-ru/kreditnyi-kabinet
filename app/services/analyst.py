"""Банковский контур: очередь, назначение, ручное решение (этап 9)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.domain.states import Actor, S
from app.domain.transitions import ActorRef, InvalidTransition, ValidationFailed, transition
from app.models import (
    Application,
    AuditLog,
    Company,
    Decision,
    InfoRequest,
    Product,
    ReviewTask,
    ScoringResult,
    StatusHistory,
    User,
    utcnow,
)
from app.services.applications import open_tasks
from app.services.credit_logic import annuity
from app.services.explain import REASONS, enqueue_rephrase, explain
from app.services.pipeline import make_offer

DECISION_KINDS = {
    "approve": (S.APPROVED, "Одобрить", "Одобрить заявку"),
    "counter": (S.COUNTEROFFER, "На иных условиях", "Отправить предложение"),
    "info": (S.NEEDS_INFO, "Запросить данные", "Запросить у клиента"),
    "decline": (S.DECLINED, "Отказать", "Отказать"),
}

DOC_KINDS = [
    "Оборотно-сальдовая ведомость по счёту 62",
    "Бухгалтерская отчётность за последний год",
    "Договор с ключевым покупателем",
    "Документы по залогу",
    "Справка об отсутствии налоговой задолженности",
    "Другое",
]


def analyst_actor(user: User) -> ActorRef:
    return ActorRef.of(user, Actor.ANALYST)


@dataclass
class QueueRow:
    app: Application
    task: ReviewTask | None
    score: ScoringResult | None
    reason: str
    overdue: bool


def queue_counts(db: Session, user: User) -> dict[str, int]:
    now = utcnow()
    base = select(func.count()).select_from(ReviewTask).join(Application).where(ReviewTask.closed_at.is_(None))
    return {
        "mine": db.scalar(base.where(ReviewTask.assigned_to == user.id, Application.status == S.MANUAL_REVIEW)) or 0,
        "unassigned": db.scalar(base.where(ReviewTask.assigned_to.is_(None), Application.status == S.MANUAL_REVIEW)) or 0,
        "waiting": db.scalar(base.where(Application.status == S.NEEDS_INFO)) or 0,
        "overdue": db.scalar(base.where(ReviewTask.due_at < now, Application.status == S.MANUAL_REVIEW)) or 0,
        "decided_today": db.scalar(
            select(func.count()).select_from(Decision).where(
                Decision.source == "ANALYST", Decision.outcome != "NEEDS_INFO",
                Decision.created_at >= now.replace(hour=0, minute=0, second=0, microsecond=0))) or 0,
    }


def queue(db: Session, user: User, *, tab: str = "mine", q: str = "", product_code: str = "") -> list[QueueRow]:
    now = utcnow()
    stmt = select(Application, ReviewTask).join(Company, Company.id == Application.company_id) \
        .join(Product, Product.id == Application.product_id)
    if tab == "decided":
        stmt = stmt.outerjoin(ReviewTask, ReviewTask.application_id == Application.id).where(
            Application.status.in_([S.APPROVED, S.COUNTEROFFER, S.DECLINED, S.APPROVED_AUTO, S.OFFER_ACCEPTED,
                                    S.SIGNING, S.DISBURSED, S.OFFER_REJECTED, S.EXPIRED]))
    elif tab == "all":
        stmt = stmt.outerjoin(ReviewTask, (ReviewTask.application_id == Application.id) & ReviewTask.closed_at.is_(None)) \
            .where(Application.status != S.DRAFT)
    else:
        stmt = stmt.join(ReviewTask, ReviewTask.application_id == Application.id).where(ReviewTask.closed_at.is_(None))
        if tab == "mine":
            stmt = stmt.where(ReviewTask.assigned_to == user.id, Application.status == S.MANUAL_REVIEW)
        elif tab == "unassigned":
            stmt = stmt.where(ReviewTask.assigned_to.is_(None), Application.status == S.MANUAL_REVIEW)
        elif tab == "waiting":
            stmt = stmt.where(Application.status == S.NEEDS_INFO)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Application.number.ilike(like), Company.inn.ilike(like), Company.name.ilike(like)))
    if product_code:
        stmt = stmt.where(Product.code == product_code)
    stmt = stmt.order_by(Application.updated_at.desc()).limit(200)
    rows: list[QueueRow] = []
    seen: set[int] = set()
    for app, task in db.execute(stmt).all():
        if app.id in seen:
            continue
        seen.add(app.id)
        sc = db.scalar(select(ScoringResult).where(ScoringResult.application_id == app.id)
                       .order_by(ScoringResult.id.desc()))
        reason = task.reason if task else ""
        if app.status == S.NEEDS_INFO:
            req = db.scalar(select(InfoRequest).where(InfoRequest.application_id == app.id, InfoRequest.status == "OPEN"))
            reason = f"Запрошено: {req.doc_kind}" if req else reason
        overdue = bool(task and task.closed_at is None and app.status == S.MANUAL_REVIEW and _aware(task.due_at) < now)
        rows.append(QueueRow(app, task, sc, reason, overdue))
    if tab in ("mine", "unassigned"):
        rows.sort(key=lambda r: (not r.overdue, -(r.task.priority if r.task else 0),
                                 _aware(r.task.due_at) if r.task else now))
    return rows


def _aware(dt):
    from datetime import timezone
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def take(db: Session, app: Application, user: User) -> ReviewTask:
    tasks = open_tasks(db, app)
    if not tasks:
        raise InvalidTransition("По заявке нет открытой задачи", code="no_task")
    t = tasks[0]
    if t.assigned_to and t.assigned_to != user.id:
        raise InvalidTransition("Заявку уже взял другой аналитик", code="already_assigned")
    t.assigned_to = user.id
    t.assigned_at = utcnow()
    db.add(AuditLog(actor_id=user.id, action="task.take", entity="application", entity_id=app.id))
    db.flush()
    return t


def release(db: Session, app: Application, user: User) -> None:
    for t in open_tasks(db, app):
        if t.assigned_to == user.id:
            t.assigned_to = None
            t.assigned_at = None
    db.add(AuditLog(actor_id=user.id, action="task.release", entity="application", entity_id=app.id))
    db.flush()


def _parse_rate(value: Any) -> Decimal:
    try:
        rate = Decimal(str(value).replace(",", ".").strip())
    except (InvalidOperation, AttributeError):
        raise ValidationFailed("Укажите ставку числом, например 21,5", code="bad_rate") from None
    if not (Decimal("0") < rate < Decimal("100")):
        raise ValidationFailed("Ставка должна быть от 0 до 100%", code="bad_rate")
    return rate.quantize(Decimal("0.01"))


def decide(
    db: Session, app: Application, user: User, *, kind: str, comment: str, expected_version: int,
    amount: int | None = None, term_months: int | None = None, rate: Any = None,
    reasons: list[str] | None = None, doc_kind: str | None = None, info_comment: str | None = None,
    due_days: int | None = None,
) -> Decision:
    if kind not in DECISION_KINDS:
        raise ValidationFailed("Неизвестный тип решения", code="bad_kind")
    if not (comment or "").strip():
        raise ValidationFailed("Комментарий обязателен — он попадёт в журнал", code="comment_required")
    target = DECISION_KINDS[kind][0]
    reasons = [r for r in (reasons or []) if r in REASONS]
    p = app.product
    actor = analyst_actor(user)

    if kind in ("approve", "counter"):
        amount = amount if amount is not None else app.amount
        term_months = term_months if term_months is not None else app.term_months
        rate_d = _parse_rate(rate)
        if not (p.min_amount <= amount <= p.max_amount):
            raise ValidationFailed(f"Сумма вне лимитов продукта ({p.min_amount:,}–{p.max_amount:,} ₽)".replace(",", " "),
                                   code="bad_amount")
        if not (p.min_term <= term_months <= p.max_term):
            raise ValidationFailed(f"Срок вне лимитов продукта ({p.min_term}–{p.max_term} мес.)", code="bad_term")
        if kind == "approve" and (amount != app.amount or term_months != app.term_months):
            kind, target = "counter", S.COUNTEROFFER  # изменили условия — значит, встречное предложение
        if kind == "counter" and not reasons:
            raise ValidationFailed("Отметьте причины для клиента", code="reasons_required")
        transition(db, app, target, event="Решение аналитика", actor=actor, reason=comment.strip(),
                   expected_version=expected_version)
        text, src = explain(target.value, reasons, p.name)
        d = Decision(application_id=app.id, outcome=target.value, source="ANALYST", decided_by=user.id,
                     reason_codes=reasons if kind == "counter" else [], comment=comment.strip(),
                     conditions={"amount": amount, "term_months": term_months, "rate": str(rate_d)},
                     explanation=text, explanation_source=src)
        db.add(d)
        db.flush()
        make_offer(db, app, d, amount=amount, term=term_months, rate=rate_d,
                   monthly=annuity(amount, rate_d, term_months))
        _close_tasks(db, app)
        enqueue_rephrase(db, app.id, target.value)
    elif kind == "decline":
        if not reasons:
            raise ValidationFailed("Отметьте причины для клиента", code="reasons_required")
        transition(db, app, S.DECLINED, event="Решение аналитика", actor=actor, reason=comment.strip(),
                   expected_version=expected_version)
        text, src = explain("DECLINED", reasons, p.name)
        d = Decision(application_id=app.id, outcome="DECLINED", source="ANALYST", decided_by=user.id,
                     reason_codes=reasons, comment=comment.strip(), explanation=text, explanation_source=src)
        db.add(d)
        _close_tasks(db, app)
        enqueue_rephrase(db, app.id, "DECLINED")
    else:  # info
        if not (doc_kind or "").strip():
            raise ValidationFailed("Выберите, что запросить у клиента", code="doc_required")
        days = due_days or settings.info_request_days
        transition(db, app, S.NEEDS_INFO, event="Запрос данных", actor=actor, reason=comment.strip(),
                   expected_version=expected_version)
        db.add(InfoRequest(application_id=app.id, requested_by=user.id, doc_kind=doc_kind.strip(),
                           comment=(info_comment or "").strip(), due_at=utcnow() + timedelta(days=days)))
        d = Decision(application_id=app.id, outcome="NEEDS_INFO", source="ANALYST", decided_by=user.id,
                     reason_codes=[], comment=comment.strip(), conditions={"doc_kind": doc_kind, "due_days": days})
        db.add(d)
    db.flush()
    return d


def _close_tasks(db: Session, app: Application) -> None:
    for t in open_tasks(db, app):
        t.closed_at = utcnow()


# --- Аналитика процесса для руководителя (B2) -------------------------------

def process_metrics(db: Session) -> dict[str, Any]:
    by_status = dict(db.execute(select(Application.status, func.count()).group_by(Application.status)).all())
    hist = db.scalars(select(StatusHistory).order_by(StatusHistory.application_id, StatusHistory.id)).all()
    durations: dict[str, list[float]] = {}
    prev: dict[int, StatusHistory] = {}
    for h in hist:
        p = prev.get(h.application_id)
        if p is not None and p.to_status:
            sec = (_aware(h.created_at) - _aware(p.created_at)).total_seconds()
            durations.setdefault(p.to_status, []).append(sec)
        prev[h.application_id] = h
    stage_avg = {k: sum(v) / len(v) for k, v in durations.items() if v}
    reasons: dict[str, int] = {}
    for d in db.scalars(select(Decision).where(Decision.outcome.in_(["DECLINED", "COUNTEROFFER"]))):
        for c in d.reason_codes or []:
            reasons[c] = reasons.get(c, 0) + 1
    auto = db.scalar(select(func.count()).select_from(Decision).where(Decision.source == "SYSTEM",
                                                                      Decision.outcome.in_(["APPROVED_AUTO", "DECLINED"]))) or 0
    manual = db.scalar(select(func.count()).select_from(Decision).where(Decision.outcome == "MANUAL_REVIEW")) or 0
    return {"by_status": by_status, "stage_avg": stage_avg, "reasons": reasons, "auto": auto, "manual": manual}
