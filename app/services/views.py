"""Сборка данных для экранов и API (чтобы HTML и JSON показывали одно и то же)."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.states import BANK_LABELS, CLIENT_LABELS, STAGES, stage_of
from app.models import (
    Application,
    ApplicationSnapshot,
    Consent,
    Contract,
    Decision,
    Disbursement,
    Document,
    ExternalCheck,
    InfoRequest,
    Offer,
    ReviewTask,
    ScoringResult,
    StatusHistory,
)
from app.services import documents
from app.services.explain import positives_for, reasons_for

# Клиентские подписи событий журнала
CLIENT_EVENT_LABELS = {
    "DRAFT": "Создан черновик",
    "SUBMITTED": "Заявка отправлена",
    "VALIDATING": "Проверяем данные заявки",
    "NEEDS_CORRECTION": "Нужно исправить данные",
    "ENRICHING": "Запрашиваем реестры и кредитную историю",
    "SCORING": "Получены данные ФНС и кредитная история",
    "MANUAL_REVIEW": "Передана специалисту",
    "NEEDS_INFO": "Банк запросил документ",
    "APPROVED_AUTO": "Заявка одобрена",
    "APPROVED": "Заявка одобрена специалистом",
    "COUNTEROFFER": "Решение специалиста: предложение на других условиях",
    "DECLINED": "Решение по заявке",
    "OFFER_ACCEPTED": "Вы приняли предложение",
    "OFFER_REJECTED": "Вы отказались от предложения",
    "SIGNING": "Документы готовы к подписанию",
    "DISBURSED": "Договор подписан, средства доступны",
    "EXPIRED": "Срок истёк",
    "WITHDRAWN": "Заявка отозвана",
}


def label(status: str, bank: bool = False) -> tuple[str, str]:
    return (BANK_LABELS if bank else CLIENT_LABELS).get(status, (status, "neutral"))


def latest(db: Session, model, app_id: int):
    return db.scalar(select(model).where(model.application_id == app_id).order_by(model.id.desc()))


def stages(app: Application) -> list[dict[str, Any]]:
    idx, problem = stage_of(app.status)
    times: dict[int, Any] = {}
    for h in app.history:
        i, _ = stage_of(h.to_status)
        if i >= 0 and i not in times:
            times[i] = h.created_at
    out = []
    for i, name in enumerate(STAGES):
        state = "done" if i < idx else ("current" if i == idx else "todo")
        if i == idx and problem:
            state = "problem"
        if idx == 6:
            state = "done"
        out.append({"name": name, "state": state, "at": times.get(i)})
    return out


def client_history(app: Application) -> list[dict[str, Any]]:
    items = []
    for h in reversed(app.history):
        if h.from_status == h.to_status:
            continue  # повторы обогащения клиенту не показываем
        items.append({"text": CLIENT_EVENT_LABELS.get(h.to_status, h.event), "at": h.created_at,
                      "who": "система" if h.actor_type == "SYSTEM" else (
                          "специалист" if h.actor_type == "ANALYST" else h.actor_name),
                      "tone": "amber" if h.to_status in ("NEEDS_INFO", "NEEDS_CORRECTION") else "blue"})
    return items


def application_view(db: Session, app: Application, *, bank: bool = False) -> dict[str, Any]:
    decision = db.scalar(select(Decision).where(Decision.application_id == app.id,
                                                Decision.outcome != "MANUAL_REVIEW").order_by(Decision.id.desc()))
    system_decision = db.scalar(select(Decision).where(Decision.application_id == app.id,
                                                       Decision.outcome == "MANUAL_REVIEW").order_by(Decision.id.desc()))
    offer = latest(db, Offer, app.id)
    score = latest(db, ScoringResult, app.id)
    info = db.scalar(select(InfoRequest).where(InfoRequest.application_id == app.id).order_by(InfoRequest.id.desc()))
    docs = db.scalars(select(Document).where(Document.application_id == app.id).order_by(Document.id)).all()
    v = {
        "app": app,
        "label": label(app.status, bank),
        "stages": stages(app),
        "decision": decision,
        "system_decision": system_decision,
        "offer": offer,
        "score": score,
        "info": info,
        "documents": docs,
        "reasons": reasons_for(decision.reason_codes if decision else []),
        "positives": positives_for(score.factors if score else []),
        "contract": db.scalar(select(Contract).where(Contract.application_id == app.id)),
        "disbursement": db.scalar(select(Disbursement).where(Disbursement.application_id == app.id)),
        "snapshot": latest(db, ApplicationSnapshot, app.id),
        "consents": db.scalars(select(Consent).where(Consent.application_id == app.id).order_by(Consent.id)).all(),
        "history": client_history(app),
    }
    if bank:
        revenue = app.financials.revenue if app.financials else None
        v["doc_checks"] = {d.id: documents.check(d.kind, d.filename, d.storage_path, d.sha256, revenue)
                           for d in docs}
        v["checks"] = db.scalars(select(ExternalCheck).where(ExternalCheck.application_id == app.id)
                                 .order_by(ExternalCheck.id)).all()
        v["task"] = db.scalar(select(ReviewTask).where(ReviewTask.application_id == app.id)
                              .order_by(ReviewTask.id.desc()))
        v["journal"] = list(reversed(app.history))
        v["hint"] = (system_decision.conditions or {}).get("hint") if system_decision and system_decision.conditions else None
    return v


def checks_summary(checks: list[ExternalCheck]) -> dict[str, dict[str, Any]]:
    """Последний ответ по каждому источнику + число неудачных попыток."""
    out: dict[str, dict[str, Any]] = {}
    for c in checks:
        key = "bki" if c.source == "BKI_MOCK" else ("egrul" if "/egrul/" in c.endpoint else "transparent")
        cur = out.setdefault(key, {"ok": False, "failures": 0, "check": None})
        if c.ok:
            cur["ok"] = True
            cur["check"] = c
        else:
            cur["failures"] += 1
            if cur["check"] is None or not cur["ok"]:
                cur["check"] = c
    return out


def history_rows(db: Session, app_id: int) -> list[StatusHistory]:
    return list(db.scalars(select(StatusHistory).where(StatusHistory.application_id == app_id)
                           .order_by(StatusHistory.id)).all())
