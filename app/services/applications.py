"""Клиентский контур заявки: черновик, согласия, отправка, ответы банку, предложение, подписание."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.domain.states import EDITABLE, OFFER_STATES, Actor, S
from app.domain.transitions import (
    ActorRef,
    Forbidden,
    InvalidTransition,
    NotFound,
    ValidationFailed,
    transition,
)
from app.models import (
    Application,
    ApplicationSnapshot,
    AuditLog,
    Company,
    CompanyMember,
    Consent,
    Contract,
    Disbursement,
    Document,
    FinancialIndicators,
    IdempotencyKey,
    InfoRequest,
    Job,
    Liability,
    Offer,
    Product,
    ReviewTask,
    StatusHistory,
    User,
    utcnow,
)
from app.services import integrations
from app.services.companies import require_member
from app.services.validation import active_consent, validate

BUREAU = "[Название БКИ] · смоделировано"

CONSENT_TEXTS: dict[str, tuple[str, str]] = {
    "PD": ("v1", "Согласие на обработку персональных данных руководителя, поручителя и сотрудников, "
                 "указанных в заявке, в целях рассмотрения заявки на кредит."),
    "BKI": ("v1", "Согласие на получение кредитного отчёта из бюро кредитных историй в целях оценки "
                  "заявки на кредит. Действует 6 месяцев; при заключении договора — весь срок договора (218-ФЗ, ст. 6)."),
    "BKI_GUARANTOR": ("v1", "Согласие поручителя на получение его кредитного отчёта в целях оценки заявки на кредит."),
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# --- Доступ -----------------------------------------------------------------

def get_application(db: Session, user: User, app_id: int) -> Application:
    """RBAC: клиент видит только заявки своих компаний; чужая заявка = 404 (не раскрываем существование)."""
    app = db.get(Application, app_id)
    if app is None:
        raise NotFound("Заявка не найдена")
    if user.role == "CLIENT":
        try:
            require_member(db, user, app.company_id)
        except NotFound:
            raise NotFound("Заявка не найдена") from None
    return app


def client_actor(user: User) -> ActorRef:
    return ActorRef.of(user, Actor.CLIENT)


# --- Черновик ---------------------------------------------------------------

def create_draft(
    db: Session, user: User, company: Company, product: Product, *,
    amount: int, term_months: int, goal: str, has_collateral: bool, has_guarantor: bool,
) -> Application:
    require_member(db, user, company.id, apply=True)
    _require_active(product)
    app = Application(
        company_id=company.id, product_id=product.id, created_by=user.id, amount=amount,
        term_months=term_months, goal=goal, has_collateral=has_collateral, has_guarantor=has_guarantor,
        status=S.DRAFT.value,
    )
    db.add(app)
    db.flush()
    app.number = f"KR-{date.today().year}-{app.id:04d}"
    fin = FinancialIndicators(application_id=app.id, employees=company.employees)
    # Не заставляем вводить повторно: берём из «Прозрачного бизнеса» и прошлой заявки
    prev = db.scalar(
        select(Application).where(Application.company_id == company.id, Application.id != app.id)
        .order_by(Application.id.desc())
    )
    if prev and prev.financials:
        pf = prev.financials
        fin.revenue, fin.expenses, fin.ebit = pf.revenue, pf.expenses, pf.ebit
        fin.amortization, fin.revenue_trend = pf.amortization, pf.revenue_trend
        for i, l in enumerate(prev.liabilities):
            db.add(Liability(application_id=app.id, position=i, creditor=l.creditor, balance=l.balance,
                             monthly_payment=l.monthly_payment, maturity=l.maturity))
    else:
        t = integrations.fns_transparent(company.inn)
        if t.ok and t.response:
            fin.revenue = t.response.get("revenue_last_year") or None
            fin.expenses = t.response.get("expenses_last_year") or None
            fin.employees = t.response.get("employees") or fin.employees
    db.add(fin)
    db.add(StatusHistory(application_id=app.id, from_status=None, to_status=S.DRAFT.value,
                         event="Создан черновик", actor_type="CLIENT", actor_id=user.id, actor_name=user.full_name))
    db.flush()
    return app


def _require_editable(app: Application) -> None:
    if app.status not in EDITABLE:
        raise InvalidTransition("Заявку уже отправили — её нельзя изменить", code="not_editable")


def _require_active(product: Product | None) -> None:
    if product is None or not product.active:
        raise ValidationFailed("Продукт не найден или больше не выдаётся", code="product_unavailable")


def update_params(db: Session, app: Application, *, amount: int | None = None, term_months: int | None = None,
                  goal: str | None = None, has_collateral: bool | None = None,
                  has_guarantor: bool | None = None, collateral_desc: str | None = None,
                  product_id: int | None = None) -> Application:
    _require_editable(app)
    if product_id is not None and product_id != app.product_id:
        product = db.get(Product, product_id)
        _require_active(product)
        app.product = product  # через связь, чтобы app.product не остался прежним до конца сессии
    for name, value in (("amount", amount), ("term_months", term_months), ("goal", goal),
                        ("has_collateral", has_collateral), ("has_guarantor", has_guarantor),
                        ("collateral_desc", collateral_desc)):
        if value is not None:
            setattr(app, name, value)
    app.updated_at = utcnow()
    db.flush()
    return app


def update_financials(db: Session, app: Application, data: dict[str, Any],
                      liabilities: list[dict[str, Any]] | None = None) -> Application:
    _require_editable(app)
    fin = app.financials or FinancialIndicators(application_id=app.id)
    for key in ("revenue", "expenses", "ebit", "amortization", "employees", "revenue_trend"):
        if key in data:
            setattr(fin, key, data[key])
    if app.financials is None:
        db.add(fin)
    if liabilities is not None:
        app.liabilities.clear()
        db.flush()
        for i, l in enumerate(liabilities):
            app.liabilities.append(Liability(position=i, creditor=l["creditor"], balance=l["balance"],
                                             monthly_payment=l.get("monthly_payment") or 0, maturity=l.get("maturity")))
    app.updated_at = utcnow()
    db.flush()
    return app


# --- Согласия ---------------------------------------------------------------

def give_consent(db: Session, app: Application, user: User, kind: str) -> Consent:
    _require_editable(app)
    existing = active_consent(db, app, kind)
    if existing:
        return existing
    version, text = CONSENT_TEXTS[kind]
    now = utcnow()
    company = app.company
    if kind == "BKI_GUARANTOR":
        founder = (company.founders or [{}])[0].get("name") or company.director or "Поручитель"
        subject = f"Поручитель {founder}"
    else:
        subject = company.name
    c = Consent(
        company_id=company.id, application_id=app.id, kind=kind, subject_name=subject,
        purpose=f"Оценка заявки {app.number}", bureau=BUREAU if kind != "PD" else None,
        given_by=user.id, given_at=now,
        valid_until=now + timedelta(days=182) if kind != "PD" else None,
        text_version=version, text_sha256=_sha(text),
        simulated=kind == "BKI_GUARANTOR",  # в MVP подпись поручителя моделируется
    )
    db.add(c)
    db.add(AuditLog(actor_id=user.id, action=f"consent.{kind.lower()}", entity="application", entity_id=app.id))
    db.flush()
    return c


def revoke_consent(db: Session, app: Application, user: User, kind: str) -> None:
    _require_editable(app)
    c = active_consent(db, app, kind)
    if c:
        c.revoked_at = utcnow()
        db.add(AuditLog(actor_id=user.id, action=f"consent.{kind.lower()}.revoke", entity="application",
                        entity_id=app.id))
        db.flush()


# --- Отправка ---------------------------------------------------------------

def build_snapshot(db: Session, app: Application) -> dict[str, Any]:
    c, f = app.company, app.financials
    consents = {k: (active_consent(db, app, k).id if active_consent(db, app, k) else None)
                for k in ("PD", "BKI", "BKI_GUARANTOR")}
    return {
        "number": app.number,
        "product": {"code": app.product.code, "version": app.product.version, "name": app.product.name},
        "amount": app.amount, "term_months": app.term_months, "goal": app.goal,
        "has_collateral": app.has_collateral, "collateral_desc": app.collateral_desc,
        "has_guarantor": app.has_guarantor,
        "company": {"inn": c.inn, "ogrn": c.ogrn, "name": c.name, "kind": c.kind, "status": c.status,
                    "registered_at": c.registered_at.isoformat() if c.registered_at else None},
        "financials": None if f is None else {
            "revenue": f.revenue, "expenses": f.expenses, "ebit": f.ebit, "amortization": f.amortization,
            "revenue_trend": f.revenue_trend, "employees": f.employees},
        "liabilities": [{"creditor": l.creditor, "balance": l.balance, "monthly_payment": l.monthly_payment,
                         "maturity": l.maturity} for l in app.liabilities],
        "consents": consents,
    }


def _idem_get(db: Session, user: User, scope: str, key: str | None) -> dict[str, Any] | None:
    if not key:
        return None
    row = db.scalar(select(IdempotencyKey).where(IdempotencyKey.user_id == user.id, IdempotencyKey.scope == scope,
                                                 IdempotencyKey.key == key))
    return row.response if row else None


def _idem_put(db: Session, user: User, scope: str, key: str | None, response: dict[str, Any]) -> None:
    if key:
        db.add(IdempotencyKey(user_id=user.id, scope=scope, key=key, response=response))


def submit(db: Session, app: Application, user: User, idempotency_key: str | None = None) -> dict[str, Any]:
    scope = f"submit:{app.id}"
    cached = _idem_get(db, user, scope, idempotency_key)
    if cached is not None:
        return cached
    require_member(db, user, app.company_id, apply=True)
    if app.status not in EDITABLE:
        raise InvalidTransition("Заявка уже отправлена", code="already_submitted")
    errors = validate(db, app)
    if errors:
        # T7: без согласия БКИ отправка блокируется, запрос в БКИ не уходит
        raise ValidationFailed("Заявку нельзя отправить: исправьте ошибки", code="validation_failed", details=errors)
    data = build_snapshot(db, app)
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False)
    app.snapshot_version += 1
    db.add(ApplicationSnapshot(application_id=app.id, version=app.snapshot_version, data=data,
                               sha256=_sha(canonical), created_by=user.id))
    app.validation_errors = None
    app.submitted_at = utcnow()
    event = "Повторная отправка" if app.status == S.NEEDS_CORRECTION else "Отправка"
    transition(db, app, S.SUBMITTED, event=event, actor=client_actor(user))
    db.add(Job(kind="PROCESS_APPLICATION", application_id=app.id))  # outbox: та же транзакция
    response = {"application_id": app.id, "number": app.number, "status": app.status,
                "snapshot_version": app.snapshot_version}
    _idem_put(db, user, scope, idempotency_key, response)
    db.flush()
    return response


def withdraw(db: Session, app: Application, user: User) -> Application:
    transition(db, app, S.WITHDRAWN, event="Отзыв заявки", actor=client_actor(user))
    for t in open_tasks(db, app):
        t.closed_at = utcnow()
    for r in db.scalars(select(InfoRequest).where(InfoRequest.application_id == app.id,
                                                  InfoRequest.status == "OPEN")):
        r.status = "CANCELLED"
    db.flush()
    return app


# --- Запрос дополнительных данных ------------------------------------------

ALLOWED_EXT = {".pdf", ".xlsx", ".xls", ".csv", ".png", ".jpg", ".jpeg", ".docx", ".doc", ".txt"}


def open_info_request(db: Session, app: Application) -> InfoRequest | None:
    return db.scalar(select(InfoRequest).where(InfoRequest.application_id == app.id, InfoRequest.status == "OPEN")
                     .order_by(InfoRequest.id.desc()))


def open_tasks(db: Session, app: Application) -> list[ReviewTask]:
    return list(db.scalars(select(ReviewTask).where(ReviewTask.application_id == app.id,
                                                    ReviewTask.closed_at.is_(None))))


def save_document(db: Session, app: Application, user: User, *, filename: str, content_type: str | None,
                  content: bytes, kind: str, info_request_id: int | None = None) -> Document:
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXT:
        raise ValidationFailed("Можно загрузить PDF, XLSX, DOCX, CSV или изображение", code="bad_file_type")
    if len(content) > settings.max_upload_mb * 1024 * 1024:
        raise ValidationFailed(f"Файл больше {settings.max_upload_mb} МБ", code="file_too_large")
    if not content:
        raise ValidationFailed("Файл пустой", code="empty_file")
    digest = hashlib.sha256(content).hexdigest()
    safe = re.sub(r"[^\w.\-]+", "_", os.path.basename(filename))[:120] or "file"
    folder = os.path.join(settings.upload_dir, str(app.id))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{digest[:16]}_{safe}")
    with open(path, "wb") as fh:
        fh.write(content)
    doc = Document(application_id=app.id, info_request_id=info_request_id, kind=kind, filename=safe,
                   content_type=content_type, size=len(content), sha256=digest, storage_path=path,
                   uploaded_by=user.id)
    db.add(doc)
    db.flush()
    return doc


def respond_info(db: Session, app: Application, user: User, *, comment: str | None,
                 files: list[tuple[str, str | None, bytes]]) -> Application:
    require_member(db, user, app.company_id, apply=True)
    req = open_info_request(db, app)
    if app.status != S.NEEDS_INFO or req is None:
        raise InvalidTransition("Банк сейчас не ждёт от вас данных", code="no_info_request")
    has_docs = db.scalar(select(Document).where(Document.info_request_id == req.id)) is not None
    if not files and not has_docs and not (comment or "").strip():
        raise ValidationFailed("Приложите документ или напишите комментарий", code="empty_answer")
    for name, ctype, content in files:
        save_document(db, app, user, filename=name, content_type=ctype, content=content, kind=req.doc_kind,
                      info_request_id=req.id)
    req.status = "ANSWERED"
    req.answer_comment = (comment or "").strip() or None
    req.answered_at = utcnow()
    req.answered_by = user.id
    transition(db, app, S.MANUAL_REVIEW, event="Ответ клиента", actor=client_actor(user),
               reason="Клиент предоставил данные")
    for t in open_tasks(db, app):
        t.due_at = utcnow() + timedelta(hours=settings.review_sla_hours)
    db.flush()
    return app


# --- Предложение и оформление ----------------------------------------------

def active_offer(db: Session, app: Application) -> Offer | None:
    return db.scalar(select(Offer).where(Offer.application_id == app.id).order_by(Offer.id.desc()))


def accept_offer(db: Session, app: Application, user: User) -> Application:
    require_member(db, user, app.company_id, apply=True)
    if app.status not in OFFER_STATES:
        raise InvalidTransition("Предложения по заявке нет", code="no_offer")
    offer = active_offer(db, app)
    if offer is None or offer.status != "ACTIVE":
        raise InvalidTransition("Предложение недействительно", code="offer_inactive")
    if _aware(offer.valid_until) < utcnow():
        raise InvalidTransition("Срок предложения истёк", code="offer_expired")
    offer.status = "ACCEPTED"
    offer.responded_at = utcnow()
    transition(db, app, S.OFFER_ACCEPTED, event="Принятие предложения", actor=client_actor(user))
    db.add(Contract(application_id=app.id, offer_id=offer.id, number=f"КД-{app.number}", status="READY"))
    transition(db, app, S.SIGNING, event="Подготовка договора", actor=ActorRef.system())
    db.flush()
    return app


def reject_offer(db: Session, app: Application, user: User) -> Application:
    require_member(db, user, app.company_id, apply=True)
    if app.status not in OFFER_STATES:
        raise InvalidTransition("Предложения по заявке нет", code="no_offer")
    offer = active_offer(db, app)
    if offer:
        offer.status = "REJECTED"
        offer.responded_at = utcnow()
    transition(db, app, S.OFFER_REJECTED, event="Отказ от предложения", actor=client_actor(user))
    db.flush()
    return app


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    m += 1
    days = [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return date(y, m, min(d.day, days[m - 1]))


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def signers(db: Session, company_id: int) -> list[User]:
    return list(db.scalars(select(User).join(CompanyMember, CompanyMember.user_id == User.id)
                           .where(CompanyMember.company_id == company_id, CompanyMember.can_sign.is_(True))
                           .order_by(User.id)))


def sign(db: Session, app: Application, user: User) -> Application:
    """Мок подписания (ПЭП) и мок выдачи. Реальные деньги не переводятся."""
    m = require_member(db, user, app.company_id)
    if app.status != S.SIGNING:
        raise InvalidTransition("Договор сейчас не ожидает подписи", code="not_signing")
    if not m.can_sign:
        names = ", ".join(s.full_name for s in signers(db, app.company_id)) or "руководитель компании"
        raise Forbidden(f"Договор подписывает сотрудник с правом подписи: {names}", code="no_sign_right")
    contract = db.scalar(select(Contract).where(Contract.application_id == app.id))
    offer = active_offer(db, app)
    assert contract is not None and offer is not None
    contract.status = "SIGNED"
    contract.signed_at = utcnow()
    contract.signed_by = user.id
    acc = f"40702 •••• {int(_sha(app.company.inn)[:8], 16) % 10000:04d}"
    db.add(Disbursement(application_id=app.id, amount=offer.amount, account_masked=acc,
                        first_payment_date=_add_months(date.today(), 1)))
    transition(db, app, S.DISBURSED, event="Подписание и выдача (смоделировано)", actor=ActorRef.system(),
               reason=f"Договор подписан: {user.full_name}")
    db.flush()
    return app
