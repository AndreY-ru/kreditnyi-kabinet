"""HTML-кабинет клиента (макеты 1–6)."""
from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.domain.states import EDITABLE, OFFER_STATES, PROCESSING, TERMINAL, S, stage_of
from app.domain.transitions import DomainError, ValidationFailed
from app.models import Application, Company, CompanyMember, Product, User, utcnow
from app.security import client_user, verify_csrf
from app.services import applications as svc
from app.services.catalog import GOALS, TERMS, match_products
from app.services.companies import add_company_by_inn, invite_member, require_member, self_check
from app.services.validation import active_consent, validate
from app.services.views import application_view, label
from app.web.common import active_company, flash, page, partial

router = APIRouter(include_in_schema=False, dependencies=[Depends(verify_csrf)])


def parse_money(v: str | None, signed: bool = False) -> int | None:
    if v is None:
        return None
    s = v.strip().replace(" ", "").replace(" ", "")
    if s == "":
        return None
    neg = signed and s.startswith("-")
    digits = re.sub(r"[^\d]", "", s)
    if not digits:
        return None
    n = int(digits)
    return -n if neg else n


def _company_ctx(request: Request, db: Session, user: User) -> dict:
    company, companies = active_company(request, db, user)
    return {"user": user, "company": company, "companies": companies, "nav": ""}


# --- Главная -------------------------------------------------------------------------

def _app_cards(db: Session, company: Company | None, tab: str) -> tuple[list[dict], dict[str, int]]:
    if company is None:
        return [], {"active": 0, "done": 0}
    apps = db.scalars(select(Application).where(Application.company_id == company.id)
                      .order_by(Application.updated_at.desc())).all()
    cards, counts = [], {"active": 0, "done": 0}
    for a in apps:
        done = a.status in TERMINAL
        counts["done" if done else "active"] += 1
        if (tab == "done") != done:
            continue
        idx, problem = stage_of(a.status)
        v = {"app": a, "label": label(a.status), "stage": idx, "problem": problem}
        if a.status in EDITABLE:
            v["filled"] = _filled(db, a)
        if a.status in OFFER_STATES:
            v["offer"] = svc.active_offer(db, a)
        if a.status == S.NEEDS_INFO:
            v["info"] = svc.open_info_request(db, a)
        cards.append(v)
    return cards, counts


def _filled(db: Session, app: Application) -> dict:
    errors = validate(db, app)
    steps = {e["step"] for e in errors}
    total = 3
    done = total - len(steps & {"params", "finance", "consents"})
    names = {"params": "параметры", "finance": "финансы", "consents": "согласия"}
    return {"pct": round(done / total * 100), "left": [names[s] for s in ("params", "finance", "consents") if s in steps]}


@router.get("/cabinet", response_class=HTMLResponse)
def home(request: Request, tab: str = "active", db: Session = Depends(get_db), user: User = Depends(client_user)):
    ctx = _company_ctx(request, db, user)
    cards, counts = _app_cards(db, ctx["company"], tab)
    member = db.scalar(select(CompanyMember).where(CompanyMember.user_id == user.id,
                                                   CompanyMember.company_id == ctx["company"].id)) if ctx["company"] else None
    attention = [c for c in cards if c["app"].status in (S.NEEDS_INFO, S.NEEDS_CORRECTION, *OFFER_STATES)
                 or (c["app"].status == S.SIGNING and member and member.can_sign)]
    team = db.scalars(select(CompanyMember).where(CompanyMember.company_id == ctx["company"].id)).all() if ctx["company"] else []
    live = any(c["app"].status in PROCESSING for c in cards)
    return page(request, "client/home.html", {**ctx, "nav": "home", "cards": cards, "counts": counts, "tab": tab,
                                              "attention": attention, "member": member, "team": team, "live": live})


@router.get("/cabinet/list", response_class=HTMLResponse)
def home_list(request: Request, tab: str = "active", db: Session = Depends(get_db), user: User = Depends(client_user)):
    company, _ = active_company(request, db, user)
    cards, counts = _app_cards(db, company, tab)
    live = any(c["app"].status in PROCESSING for c in cards)
    return partial(request, "partials/app_list.html", {"cards": cards, "counts": counts, "tab": tab, "live": live})


@router.post("/company/switch")
def switch_company(request: Request, company_id: int = Form(...), db: Session = Depends(get_db),
                   user: User = Depends(client_user)):
    require_member(db, user, company_id)
    request.session["company_id"] = company_id
    return RedirectResponse("/cabinet", 303)


def _company_page_ctx(request: Request, db: Session, user: User, **extra) -> dict:
    ctx = _company_ctx(request, db, user)
    team, me = [], None
    if ctx["company"]:
        team = db.scalars(select(CompanyMember).where(CompanyMember.company_id == ctx["company"].id)
                          .order_by(CompanyMember.id)).all()
        me = next((m for m in team if m.user_id == user.id), None)
    return {**ctx, "nav": "company", "error": None, "inn": "", "invite_error": None, "team": team, "me": me,
            **extra}


@router.get("/company", response_class=HTMLResponse)
def company_page(request: Request, db: Session = Depends(get_db), user: User = Depends(client_user)):
    return page(request, "client/company.html", _company_page_ctx(request, db, user))


@router.post("/company/add")
def company_add(request: Request, inn: str = Form(...), position: str = Form("Представитель"),
                db: Session = Depends(get_db), user: User = Depends(client_user)):
    try:
        c = add_company_by_inn(db, user, inn, position)
    except DomainError as e:
        db.rollback()
        return page(request, "client/company.html", _company_page_ctx(request, db, user, error=e.message, inn=inn),
                    status_code=e.status_code)
    db.commit()
    request.session["company_id"] = c.id
    flash(request, f"Компания {c.name} добавлена. Данные взяты из ЕГРЮЛ (смоделировано).")
    return RedirectResponse("/cabinet", 303)


@router.post("/company/invite")
def company_invite(request: Request, email: str = Form(...), position: str = Form(""),
                   can_sign: str | None = Form(None), db: Session = Depends(get_db),
                   user: User = Depends(client_user)):
    company, _ = active_company(request, db, user)
    if company is None:
        return RedirectResponse("/company", 303)
    try:
        m = invite_member(db, user, company.id, email=email, position=position.strip() or "Сотрудник",
                          can_apply=True, can_sign=bool(can_sign))
    except DomainError as e:
        db.rollback()
        return page(request, "client/company.html", _company_page_ctx(request, db, user, invite_error=e.message),
                    status_code=e.status_code)
    db.commit()
    flash(request, f"{m.user.full_name} теперь в команде {company.name}")
    return RedirectResponse("/company", 303)


# --- Подбор ---------------------------------------------------------------------------

def _podbor_params(goal: str, amount: str, term: int, collateral: str | None, guarantor: str | None) -> dict:
    a = parse_money(amount) or 0
    return {"goal": goal if goal in GOALS else "working", "amount": a, "amount_raw": amount, "term": term,
            "collateral": bool(collateral), "guarantor": bool(guarantor)}


@router.get("/podbor", response_class=HTMLResponse)
def podbor(request: Request, goal: str = "working", amount: str = "5 000 000", term: int = 24,
           collateral: str | None = None, guarantor: str | None = "1", app_id: int | None = None,
           db: Session = Depends(get_db), user: User = Depends(client_user)):
    ctx = _company_ctx(request, db, user)
    draft = None
    if app_id:
        draft = svc.get_application(db, user, app_id)
        if draft.status not in EDITABLE:
            return RedirectResponse(f"/applications/{draft.id}", 303)
        goal, amount, term = draft.goal, f"{draft.amount:,}".replace(",", " "), draft.term_months
        collateral = "1" if draft.has_collateral else None
        guarantor = "1" if draft.has_guarantor else None
    params = _podbor_params(goal, amount, term, collateral, guarantor)
    matches = _matches(db, params, ctx["company"])
    check = self_check(db, ctx["company"]) if ctx["company"] else None
    return page(request, "client/podbor.html", {**ctx, "nav": "podbor", "p": params, "matches": matches,
                                                "check": check, "terms": TERMS, "draft": draft})


def _matches(db: Session, p: dict, company: Company | None):
    if p["amount"] <= 0:
        return []
    return match_products(db, goal=p["goal"], amount=p["amount"], term=p["term"], has_collateral=p["collateral"],
                          has_guarantor=p["guarantor"], company=company)


@router.get("/podbor/results", response_class=HTMLResponse)
def podbor_results(request: Request, goal: str = "working", amount: str = "", term: int = 24,
                   collateral: str | None = None, guarantor: str | None = None, app_id: int | None = None,
                   db: Session = Depends(get_db), user: User = Depends(client_user)):
    company, _ = active_company(request, db, user)
    params = _podbor_params(goal, amount, term, collateral, guarantor)
    draft = svc.get_application(db, user, app_id) if app_id else None
    return partial(request, "partials/podbor_results.html",
                   {"p": params, "matches": _matches(db, params, company), "draft": draft})


@router.post("/applications/new")
def new_application(request: Request, product_id: int = Form(...), goal: str = Form("working"),
                    amount: str = Form(...), term: int = Form(...), collateral: str | None = Form(None),
                    guarantor: str | None = Form(None), app_id: int | None = Form(None),
                    db: Session = Depends(get_db), user: User = Depends(client_user)):
    company, _ = active_company(request, db, user)
    product = db.get(Product, product_id)
    if company is None or product is None:
        raise ValidationFailed("Сначала добавьте компанию")
    p = _podbor_params(goal, amount, term, collateral, guarantor)
    if app_id:
        app = svc.get_application(db, user, app_id)
        svc.update_params(db, app, amount=p["amount"], term_months=p["term"], goal=p["goal"],
                          has_collateral=p["collateral"], has_guarantor=p["guarantor"], product_id=product.id)
    else:
        app = svc.create_draft(db, user, company, product, amount=p["amount"], term_months=p["term"], goal=p["goal"],
                               has_collateral=p["collateral"], has_guarantor=p["guarantor"])
    db.commit()
    return RedirectResponse(f"/applications/{app.id}/edit", 303)


# --- Заявка: компания и финансы ------------------------------------------------------------

def _form_financials(form) -> tuple[dict, list[dict]]:
    data = {
        "revenue": parse_money(form.get("revenue")),
        "expenses": parse_money(form.get("expenses")),
        "ebit": parse_money(form.get("ebit"), signed=True),
        "amortization": parse_money(form.get("amortization")),
        "employees": parse_money(form.get("employees")),
        "revenue_trend": form.get("revenue_trend") or None,
    }
    creditors = form.getlist("l_creditor")
    balances = form.getlist("l_balance")
    payments = form.getlist("l_payment")
    maturities = form.getlist("l_maturity")
    liabs = []
    for i, cred in enumerate(creditors):
        bal = parse_money(balances[i] if i < len(balances) else "")
        if not cred.strip() and bal is None:
            continue
        liabs.append({"creditor": cred.strip(), "balance": bal or 0,
                      "monthly_payment": parse_money(payments[i] if i < len(payments) else "") or 0,
                      "maturity": (maturities[i] if i < len(maturities) else "").strip() or None})
    return data, liabs


def _edit_ctx(request: Request, db: Session, user: User, app: Application, show_errors: bool) -> dict:
    ctx = _company_ctx(request, db, user)
    errors = [e for e in validate(db, app) if e["step"] in ("params", "finance")]
    member = db.scalar(select(CompanyMember).where(CompanyMember.user_id == user.id,
                                                   CompanyMember.company_id == app.company_id))
    return {**ctx, "app": app, "company": app.company, "member": member,
            "errors": {e["field"]: e["message"] for e in errors} if show_errors else {},
            "error_count": len(errors), "show_errors": show_errors, "step": 3}


@router.get("/applications/{app_id}/edit", response_class=HTMLResponse)
def edit(request: Request, app_id: int, check: int = 0, db: Session = Depends(get_db),
         user: User = Depends(client_user)):
    app = svc.get_application(db, user, app_id)
    if app.status not in EDITABLE:
        return RedirectResponse(f"/applications/{app.id}", 303)
    return page(request, "client/zayavka.html", _edit_ctx(request, db, user, app, bool(check)))


@router.post("/applications/{app_id}/edit")
async def edit_save(request: Request, app_id: int, db: Session = Depends(get_db), user: User = Depends(client_user)):
    app = svc.get_application(db, user, app_id)
    form = await request.form()
    data, liabs = _form_financials(form)
    svc.update_financials(db, app, data, liabs)
    db.commit()
    action = form.get("action", "save")
    if action == "autosave":
        return partial(request, "partials/saved.html", {"at": utcnow()})
    if action == "next":
        errors = [e for e in validate(db, app) if e["step"] in ("params", "finance")]
        if errors:
            return page(request, "client/zayavka.html", _edit_ctx(request, db, user, app, True), status_code=422)
        return RedirectResponse(f"/applications/{app.id}/consents", 303)
    flash(request, "Черновик сохранён")
    return RedirectResponse("/cabinet", 303)


# --- Согласия и отправка ----------------------------------------------------------------

def _consents_ctx(request: Request, db: Session, user: User, app: Application, errors: list | None = None) -> dict:
    ctx = _company_ctx(request, db, user)
    consents = {k: active_consent(db, app, k) for k in ("PD", "BKI", "BKI_GUARANTOR")}
    need_guarantor = app.has_guarantor and app.company.kind == "OOO"
    all_errors = validate(db, app)
    other = [e for e in all_errors if e["step"] != "consents"]
    ready = not all_errors
    return {**ctx, "app": app, "company": app.company, "consents": consents, "need_guarantor": need_guarantor,
            "ready": ready, "other_errors": other, "errors": errors or [], "step": 4,
            "idem": uuid.uuid4().hex, "texts": svc.CONSENT_TEXTS}


@router.get("/applications/{app_id}/consents", response_class=HTMLResponse)
def consents(request: Request, app_id: int, db: Session = Depends(get_db), user: User = Depends(client_user)):
    app = svc.get_application(db, user, app_id)
    if app.status not in EDITABLE:
        return RedirectResponse(f"/applications/{app.id}", 303)
    return page(request, "client/soglasiya.html", _consents_ctx(request, db, user, app))


@router.post("/applications/{app_id}/consents/{kind}", response_class=HTMLResponse)
def toggle_consent(request: Request, app_id: int, kind: str, value: str | None = Form(None),
                   db: Session = Depends(get_db), user: User = Depends(client_user)):
    app = svc.get_application(db, user, app_id)
    if kind not in svc.CONSENT_TEXTS:
        raise ValidationFailed("Неизвестный вид согласия")
    if value:
        svc.give_consent(db, app, user, kind)
    else:
        svc.revoke_consent(db, app, user, kind)
    db.commit()
    if request.headers.get("HX-Request"):
        return partial(request, "partials/submit_panel.html", _consents_ctx(request, db, user, app))
    return RedirectResponse(f"/applications/{app.id}/consents", 303)


@router.post("/applications/{app_id}/submit")
def submit(request: Request, app_id: int, idem: str = Form(""), db: Session = Depends(get_db),
           user: User = Depends(client_user)):
    app = svc.get_application(db, user, app_id)
    try:
        svc.submit(db, app, user, idem or None)
    except ValidationFailed as e:
        db.rollback()
        return page(request, "client/soglasiya.html",
                    _consents_ctx(request, db, user, app, errors=e.details or [{"message": e.message}]),
                    status_code=422)
    except DomainError:
        db.rollback()
        return RedirectResponse(f"/applications/{app.id}", 303)  # уже отправлена (двойной клик)
    db.commit()
    return RedirectResponse(f"/applications/{app.id}", 303)


# --- Статус и результат -------------------------------------------------------------------

@router.get("/applications/{app_id}", response_class=HTMLResponse)
def status(request: Request, app_id: int, db: Session = Depends(get_db), user: User = Depends(client_user)):
    app = svc.get_application(db, user, app_id)
    if app.status == S.DRAFT:
        return RedirectResponse(f"/applications/{app.id}/edit", 303)
    ctx = _company_ctx(request, db, user)
    v = application_view(db, app)
    alts = []
    if v["decision"] and v["decision"].conditions:
        alts = [db.get(Product, a["product_id"]) for a in v["decision"].conditions.get("alternatives", [])]
    team = db.scalars(select(CompanyMember).where(CompanyMember.company_id == app.company_id)).all()
    return page(request, "client/status.html", {**ctx, "v": v, "app": app, "alts": [a for a in alts if a],
                                                "team": team, "live": app.status in PROCESSING})


@router.get("/applications/{app_id}/live", response_class=HTMLResponse)
def status_live(request: Request, app_id: int, since: str = "", db: Session = Depends(get_db),
                user: User = Depends(client_user)):
    app = svc.get_application(db, user, app_id)
    if since and since != app.status:
        # статус сменился — перерисовываем страницу целиком
        resp = HTMLResponse("")
        resp.headers["HX-Refresh"] = "true"
        return resp
    v = application_view(db, app)
    return partial(request, "partials/stages.html", {"v": v, "app": app, "live": app.status in PROCESSING})


@router.post("/applications/{app_id}/withdraw")
def withdraw(request: Request, app_id: int, db: Session = Depends(get_db), user: User = Depends(client_user)):
    app = svc.get_application(db, user, app_id)
    svc.withdraw(db, app, user)
    db.commit()
    flash(request, f"Заявка {app.number} отозвана", "info")
    return RedirectResponse("/cabinet", 303)


@router.post("/applications/{app_id}/info")
async def info_answer(request: Request, app_id: int, comment: str = Form(""),
                      db: Session = Depends(get_db), user: User = Depends(client_user)):
    app = svc.get_application(db, user, app_id)
    form = await request.form()
    uploads = [f for f in form.getlist("files") if hasattr(f, "filename") and f.filename]
    payload = [(f.filename, f.content_type, await f.read()) for f in uploads]
    try:
        svc.respond_info(db, app, user, comment=comment, files=payload)
    except ValidationFailed as e:
        db.rollback()
        flash(request, e.message, "err")
        return RedirectResponse(f"/applications/{app.id}", 303)
    db.commit()
    flash(request, "Ответ отправлен. Заявка вернулась к специалисту.")
    return RedirectResponse(f"/applications/{app.id}", 303)


@router.post("/applications/{app_id}/accept")
def accept(request: Request, app_id: int, db: Session = Depends(get_db), user: User = Depends(client_user)):
    app = svc.get_application(db, user, app_id)
    svc.accept_offer(db, app, user)
    db.commit()
    return RedirectResponse(f"/applications/{app.id}", 303)


@router.post("/applications/{app_id}/reject")
def reject(request: Request, app_id: int, db: Session = Depends(get_db), user: User = Depends(client_user)):
    app = svc.get_application(db, user, app_id)
    svc.reject_offer(db, app, user)
    db.commit()
    flash(request, "Вы отказались от предложения", "info")
    return RedirectResponse(f"/applications/{app.id}", 303)


@router.post("/applications/{app_id}/sign")
def sign(request: Request, app_id: int, db: Session = Depends(get_db), user: User = Depends(client_user)):
    app = svc.get_application(db, user, app_id)
    svc.sign(db, app, user)
    db.commit()
    return RedirectResponse(f"/applications/{app.id}", 303)
