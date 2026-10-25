"""HTML-кабинет банка (макеты 7–8) + процесс и настройки."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.domain.states import S
from app.domain.transitions import DomainError, NotFound
from app.models import AuditLog, InfoRequest, Product, ScoringModel, User
from app.security import admin_user, bank_user, verify_csrf
from app.services import analyst as an
from app.services import applications as svc
from app.services import client_message
from app.services import products as products_svc
from app.services.credit_logic import activate_model, credit_cfg, rate_for
from app.services.explain import REASONS
from app.services.pipeline import active_model
from app.services.views import application_view, checks_summary
from app.web.client import parse_money
from app.web.common import flash, page, partial

router = APIRouter(prefix="/bank", include_in_schema=False, dependencies=[Depends(verify_csrf)])


@router.get("/queue", response_class=HTMLResponse)
def queue(request: Request, tab: str = "mine", q: str = "", product: str = "", db: Session = Depends(get_db),
          user: User = Depends(bank_user)):
    rows = an.queue(db, user, tab=tab, q=q, product_code=product)
    products = db.scalars(select(Product).order_by(Product.id)).all()
    return page(request, "bank/queue.html", {"user": user, "nav": "all" if tab == "all" else "queue", "rows": rows,
                                             "tab": tab, "q": q,
                                             "product": product, "products": products,
                                             "counts": an.queue_counts(db, user)})


@router.get("/applications/{app_id}", response_class=HTMLResponse)
def card(request: Request, app_id: int, d: str = "", db: Session = Depends(get_db), user: User = Depends(bank_user)):
    app = svc.get_application(db, user, app_id)
    v = application_view(db, app, bank=True)
    cfg = active_model(db)
    hint = v["hint"]
    default_kind = d or ("counter" if hint and hint.get("amount", app.amount) < app.amount else "approve")
    sc = v["score"]
    suggested_rate = rate_for(app.product, sc.score, cfg, (sc.inputs or {}).get("lgd")) if sc else app.product.base_rate
    answered = db.scalar(select(InfoRequest).where(InfoRequest.application_id == app.id,
                                                   InfoRequest.status == "ANSWERED").order_by(InfoRequest.id.desc()))
    return page(request, "bank/card.html", {
        "user": user, "nav": "queue", "v": v, "app": app, "checks": checks_summary(v["checks"]),
        "cfg": cfg, "kind": default_kind, "kinds": an.DECISION_KINDS, "reasons": REASONS, "doc_kinds": an.DOC_KINDS,
        "suggested_rate": suggested_rate, "answered": answered, "form": {}, "error": None,
        "can_decide": app.status == S.MANUAL_REVIEW,
    })


@router.post("/applications/{app_id}/take")
def take(request: Request, app_id: int, db: Session = Depends(get_db), user: User = Depends(bank_user)):
    app = svc.get_application(db, user, app_id)
    try:
        an.take(db, app, user)
        db.commit()
        flash(request, "Заявка назначена на вас")
    except DomainError as e:
        db.rollback()
        flash(request, e.message, "err")
    return RedirectResponse(f"/bank/applications/{app.id}", 303)


@router.post("/applications/{app_id}/release")
def release(request: Request, app_id: int, db: Session = Depends(get_db), user: User = Depends(bank_user)):
    app = svc.get_application(db, user, app_id)
    an.release(db, app, user)
    db.commit()
    flash(request, "Заявка возвращена в общую очередь", "info")
    return RedirectResponse("/bank/queue", 303)


@router.post("/applications/{app_id}/decision")
async def decision(request: Request, app_id: int, db: Session = Depends(get_db), user: User = Depends(bank_user)):
    app = svc.get_application(db, user, app_id)
    form = await request.form()
    kind = str(form.get("kind") or "")
    try:
        expected = int(str(form.get("version") or "0"))
        an.decide(db, app, user, kind=kind, comment=str(form.get("comment") or ""), expected_version=expected,
                  amount=parse_money(str(form.get("amount") or "")), term_months=parse_money(str(form.get("term") or "")),
                  rate=form.get("rate"), reasons=[str(r) for r in form.getlist("reasons")],
                  doc_kind=str(form.get("doc_kind") or ""), info_comment=str(form.get("info_comment") or ""),
                  due_days=parse_money(str(form.get("due_days") or "")))
        db.commit()
    except DomainError as e:
        db.rollback()
        app = svc.get_application(db, user, app_id)
        v = application_view(db, app, bank=True)
        cfg = active_model(db)
        sc = v["score"]
        return page(request, "bank/card.html", {
            "user": user, "nav": "queue", "v": v, "app": app, "checks": checks_summary(v["checks"]), "cfg": cfg,
            "kind": kind, "kinds": an.DECISION_KINDS, "reasons": REASONS, "doc_kinds": an.DOC_KINDS,
            "suggested_rate": rate_for(app.product, sc.score, cfg, (sc.inputs or {}).get("lgd")) if sc
            else app.product.base_rate,
            "answered": None, "form": dict(form) | {"reasons": form.getlist("reasons")}, "error": e.message,
            "conflict": e.code == "version_conflict", "can_decide": app.status == S.MANUAL_REVIEW,
        }, status_code=e.status_code)
    flash(request, f"Решение по {app.number} сохранено: {an.DECISION_KINDS[kind][1].lower()}")
    return RedirectResponse(f"/bank/applications/{app.id}", 303)


@router.post("/applications/{app_id}/client-message", response_class=HTMLResponse)
def client_message_draft(request: Request, app_id: int, doc_kind: str = Form(""), info_note: str = Form(""),
                         db: Session = Depends(get_db), user: User = Depends(bank_user)):
    """Черновик пояснения для клиента по короткой заметке аналитика. Отправляет аналитик сам."""
    svc.get_application(db, user, app_id)
    d = client_message.draft(info_note, doc_kind)
    return partial(request, "partials/client_message.html", {"text": d.text, "source": d.source,
                                                             "rejected": d.rejected})


@router.get("/process", response_class=HTMLResponse)
def process(request: Request, db: Session = Depends(get_db), user: User = Depends(bank_user)):
    return page(request, "bank/process.html", {"user": user, "nav": "process", "m": an.process_metrics(db),
                                               "reasons": REASONS})


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db), user: User = Depends(bank_user)):
    products = db.scalars(select(Product).order_by(Product.id)).all()
    models = db.scalars(select(ScoringModel).order_by(ScoringModel.id.desc())).all()
    cfg = active_model(db)
    journal = db.scalars(select(AuditLog).where(AuditLog.action.like("product.%") | AuditLog.action.like("sandbox.%")
                                                | AuditLog.action.like("scoring.%"))
                         .order_by(AuditLog.id.desc()).limit(30)).all()
    names = {u.id: u.full_name for u in db.scalars(select(User).where(User.id.in_({j.actor_id for j in journal})))}
    return page(request, "bank/settings.html", {"user": user, "nav": "settings", "products": products,
                                                "models": models, "cfg": cfg, "credit": credit_cfg(cfg),
                                                "journal": journal, "names": names, "kinds": products_svc.KINDS})


@router.post("/settings/models/{version}/activate")
def model_activate(request: Request, version: str, db: Session = Depends(get_db), user: User = Depends(admin_user)):
    try:
        activate_model(db, user, version)
    except DomainError as e:
        db.rollback()
        flash(request, e.message, "err")
        return RedirectResponse("/bank/settings", 303)
    db.commit()
    flash(request, f"Активна версия карты {version}: по ней посчитаются новые заявки")
    return RedirectResponse("/bank/settings", 303)


_BLANK_PRODUCT = {"code": "", "name": "", "kind": "LOAN", "description": "", "goals": ["working"],
                  "min_amount": 500_000, "max_amount": 5_000_000, "min_term": 6, "max_term": 36,
                  "min_business_months": 12, "requires_collateral": False, "unsecured_max": None,
                  "requires_guarantor": True, "base_rate": "19,00", "auto_limit": 1_000_000, "decision_days": 3,
                  "gov_support": False, "active": True}


def _product_form(form) -> dict:
    data = {k: form.get(k) for k in ("code", "name", "kind", "description", "min_amount", "max_amount", "min_term",
                                     "max_term", "min_business_months", "unsecured_max", "base_rate", "auto_limit",
                                     "decision_days")}
    data["goals"] = form.getlist("goals")
    for k in ("requires_collateral", "requires_guarantor", "gov_support", "active"):
        data[k] = form.get(k)
    return data


def _product_page(request: Request, user: User, p: dict, product: Product | None, errors: dict | None = None,
                  status_code: int = 200) -> HTMLResponse:
    return page(request, "bank/product.html", {"user": user, "nav": "settings", "p": p, "product": product,
                                               "errors": errors or {}, "kinds": products_svc.KINDS},
                status_code=status_code)


@router.get("/settings/products/new", response_class=HTMLResponse)
def product_new(request: Request, user: User = Depends(admin_user)):
    return _product_page(request, user, dict(_BLANK_PRODUCT), None)


@router.post("/settings/products/new")
async def product_create(request: Request, db: Session = Depends(get_db), user: User = Depends(admin_user)):
    data = _product_form(await request.form())
    try:
        p = products_svc.save_product(db, user, data)
    except DomainError as e:
        db.rollback()
        return _product_page(request, user, {**data, "active": bool(data["active"])}, None, e.details or {}, 422)
    db.commit()
    flash(request, f"Продукт «{p.name}» добавлен — он уже в подборе и проверке шансов" if p.active
          else f"Продукт «{p.name}» добавлен в архив")
    return RedirectResponse("/bank/settings", 303)


@router.get("/settings/products/{product_id}", response_class=HTMLResponse)
def product_edit(request: Request, product_id: int, db: Session = Depends(get_db), user: User = Depends(admin_user)):
    product = db.get(Product, product_id)
    if product is None:
        raise NotFound("Продукт не найден")
    return _product_page(request, user, products_svc.as_dict(product), product)


@router.post("/settings/products/{product_id}")
async def product_save(request: Request, product_id: int, db: Session = Depends(get_db),
                       user: User = Depends(admin_user)):
    product = db.get(Product, product_id)
    if product is None:
        raise NotFound("Продукт не найден")
    data = _product_form(await request.form())
    try:
        before = product.version
        products_svc.save_product(db, user, data, product)
    except DomainError as e:
        db.rollback()
        return _product_page(request, user, {**data, "code": product.code}, product, e.details or {}, 422)
    db.commit()
    flash(request, f"«{product.name}» сохранён, версия {product.version}" if product.version != before
          else "Изменений нет")
    return RedirectResponse("/bank/settings", 303)
