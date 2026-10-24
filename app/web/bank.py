"""HTML-кабинет банка (макеты 7–8) + процесс и настройки."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.domain.states import S
from app.domain.transitions import DomainError
from app.models import AuditLog, InfoRequest, Product, ScoringModel, User
from app.security import admin_user, bank_user, verify_csrf
from app.services import analyst as an
from app.services import applications as svc
from app.services import client_message
from app.services.credit_logic import rate_for
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
    suggested_rate = rate_for(app.product, sc.score, cfg) if sc else app.product.base_rate
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
            "suggested_rate": rate_for(app.product, sc.score, cfg) if sc else app.product.base_rate,
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
    return page(request, "bank/settings.html", {"user": user, "nav": "settings", "products": products,
                                                "models": models, "cfg": active_model(db)})


@router.post("/settings/products/{product_id}")
def settings_product(request: Request, product_id: int, base_rate: str = Form(...), auto_limit: str = Form(...),
                     active: str | None = Form(None), db: Session = Depends(get_db), user: User = Depends(admin_user)):
    p = db.get(Product, product_id)
    if p is None:
        raise DomainError("Продукт не найден")
    try:
        p.base_rate = an._parse_rate(base_rate)
    except DomainError as e:
        flash(request, e.message, "err")
        return RedirectResponse("/bank/settings", 303)
    p.auto_limit = parse_money(auto_limit) or 0
    p.active = bool(active)
    p.version += 1
    db.add(AuditLog(actor_id=user.id, action="product.update", entity="product", entity_id=p.id,
                    data={"base_rate": str(p.base_rate), "auto_limit": p.auto_limit, "active": p.active}))
    db.commit()
    flash(request, f"«{p.name}» обновлён, версия {p.version}")
    return RedirectResponse("/bank/settings", 303)
