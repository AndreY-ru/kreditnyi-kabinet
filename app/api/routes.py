"""JSON API (документируется в OpenAPI: /api/docs). Вся бизнес-логика — в app.services."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import schemas as s
from app.db import get_db
from app.models import Application, AuditLog, Company, CompanyMember, Product, ScoringModel, User
from app.security import TooManyAttempts, admin_user, authenticate, bank_user, client_user, current_user, start_session
from app.services import analyst as analyst_svc
from app.services import applications as svc
from app.services import accounts, client_message, intake, precheck
from app.services.catalog import match_products
from app.services.companies import add_company_by_inn, invite_member, require_member, self_check, user_companies
from app.services.views import application_view, history_rows, label

router = APIRouter(prefix="/api")


def app_out(db: Session, app: Application) -> s.ApplicationOut:
    v = application_view(db, app)
    offer = v["offer"] if v["offer"] and app.status in ("APPROVED_AUTO", "APPROVED", "COUNTEROFFER", "OFFER_ACCEPTED",
                                                         "SIGNING", "DISBURSED") else None
    info = v["info"]
    return s.ApplicationOut(
        id=app.id, number=app.number, status=app.status, status_label=label(app.status)[0], version=app.version,
        company_id=app.company_id, product_id=app.product_id, product_name=app.product.name, amount=app.amount,
        term_months=app.term_months, goal=app.goal, has_collateral=app.has_collateral, has_guarantor=app.has_guarantor,
        snapshot_version=app.snapshot_version, created_at=app.created_at, submitted_at=app.submitted_at,
        validation_errors=app.validation_errors,
        offer=s.OfferOut.model_validate(offer) if offer else None,
        reasons=[s.ReasonOut(**r) for r in v["reasons"]],
        explanation=v["decision"].explanation if v["decision"] else None,
        info_request=None if not info or info.status != "OPEN" else {
            "doc_kind": info.doc_kind, "comment": info.comment, "due_at": info.due_at.isoformat()},
    )


# --- Аутентификация -----------------------------------------------------------

@router.post("/auth/login", response_model=s.UserOut, tags=["auth"])
def login(body: s.LoginIn, request: Request, db: Session = Depends(get_db)):
    try:
        user = authenticate(db, body.email, body.password, request.client.host if request.client else None)
    except TooManyAttempts as e:
        raise HTTPException(429, str(e), headers={"Retry-After": str(e.retry_after)}) from None
    if user is None:
        raise HTTPException(401, "Неверный email или пароль")
    start_session(request, db, user)
    return user


@router.post("/auth/register", response_model=s.UserOut, status_code=201, tags=["auth"],
             responses={422: {"model": s.Error}, 429: {"model": s.Error}})
def register(body: s.RegisterIn, request: Request, db: Session = Depends(get_db)):
    """Регистрация представителя бизнеса. Дальше — POST /api/companies с ИНН."""
    try:
        user = accounts.register(db, full_name=body.full_name, email=body.email, password=body.password,
                                 pd_consent=body.pd_consent, ip=request.client.host if request.client else None)
    except TooManyAttempts as e:
        raise HTTPException(429, str(e), headers={"Retry-After": str(e.retry_after)}) from None
    start_session(request, db, user)
    return user


@router.post("/auth/logout", status_code=204, tags=["auth"])
def logout(request: Request):
    request.session.clear()


@router.get("/auth/me", response_model=s.UserOut, tags=["auth"])
def me(user: User = Depends(current_user)):
    return user


# --- Каталог и подбор -----------------------------------------------------------

@router.get("/products", response_model=list[s.ProductOut], tags=["catalog"])
def products(db: Session = Depends(get_db)):
    return db.scalars(select(Product).where(Product.active.is_(True)).order_by(Product.id)).all()


@router.post("/products/match", response_model=list[s.MatchOut], tags=["catalog"])
def match(body: s.MatchIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    company = None
    if body.company_id:
        require_member(db, user, body.company_id)
        company = db.get(Company, body.company_id)
    res = match_products(db, goal=body.goal, amount=body.amount, term=body.term_months,
                         has_collateral=body.has_collateral, has_guarantor=body.has_guarantor, company=company)
    return [s.MatchOut(product=s.ProductOut.model_validate(m.product), eligible=m.eligible, fit=m.fit,
                       reasons=m.reasons, warnings=m.warnings) for m in res]


# --- Компании -----------------------------------------------------------------------

@router.post("/podbor/parse", response_model=s.IntakeOut, tags=["catalog"])
def podbor_parse(body: s.IntakeIn, user: User = Depends(current_user)):
    """Задача своими словами → параметры подбора. Модель (если включена) понимает контекст, но числа берёт
    только из текста; без модели — правила. Дальше — обычный /products/match."""
    r = intake.parse(body.text)
    return s.IntakeOut(goal=r.goal, amount=r.amount, term_months=r.term, has_collateral=r.collateral,
                       has_guarantor=r.guarantor, source=r.source, missing=r.missing, summary=r.summary())


@router.get("/companies", response_model=list[s.CompanyOut], tags=["companies"])
def my_companies(db: Session = Depends(get_db), user: User = Depends(client_user)):
    return user_companies(db, user)


@router.post("/companies", response_model=s.CompanyOut, status_code=201, tags=["companies"])
def add_company(body: s.CompanyIn, db: Session = Depends(get_db), user: User = Depends(client_user)):
    c = add_company_by_inn(db, user, body.inn, body.position)
    db.commit()
    return c


@router.get("/companies/{company_id}/self-check", response_model=list[s.SelfCheckItem], tags=["companies"])
def company_self_check(company_id: int, db: Session = Depends(get_db), user: User = Depends(client_user)):
    require_member(db, user, company_id)
    return self_check(db, db.get(Company, company_id))["items"]


@router.post("/companies/{company_id}/precheck", response_model=s.PrecheckOut, tags=["companies"])
def company_precheck(company_id: int, body: s.PrecheckIn, db: Session = Depends(get_db),
                     user: User = Depends(client_user)):
    """Шансы до подачи: та же модель, что в конвейере, но без БКИ и без записи в БД. Баллы и пороги не раскрываются."""
    require_member(db, user, company_id)
    company = db.get(Company, company_id)
    d = precheck.defaults(db, company)
    p = precheck.PrecheckParams(
        goal=body.goal, amount=body.amount, term=body.term_months, has_collateral=body.has_collateral,
        has_guarantor=body.has_guarantor, revenue=body.revenue if body.revenue is not None else d["revenue"],
        profit=body.profit if body.profit is not None else d["profit"],
        debt=body.debt if body.debt is not None else d["debt"], trend=body.revenue_trend or d["trend"],
        overdues=body.overdues)
    r = precheck.run(db, company, p)
    if not r["ready"]:
        raise HTTPException(422, "Нужны сумма, выручка и прибыль")
    return s.PrecheckOut(level=r["level"], factors=r["items"], tips=r["tips"], comfortable_amount=r["cap"],
                         products=[s.PrecheckProduct(product_id=x["product"].id, name=x["product"].name,
                                                     verdict=x["verdict"], notes=x["notes"]) for x in r["products"]])


@router.get("/companies/{company_id}/members", response_model=list[s.MemberOut], tags=["companies"])
def company_members(company_id: int, db: Session = Depends(get_db), user: User = Depends(client_user)):
    require_member(db, user, company_id)
    rows = db.scalars(select(CompanyMember).where(CompanyMember.company_id == company_id)
                      .order_by(CompanyMember.id)).all()
    return [s.MemberOut(user_id=m.user_id, full_name=m.user.full_name, position=m.position, can_apply=m.can_apply,
                        can_sign=m.can_sign) for m in rows]


@router.post("/companies/{company_id}/members", response_model=s.MemberOut, status_code=201, tags=["companies"])
def company_invite(company_id: int, body: s.MemberIn, db: Session = Depends(get_db),
                   user: User = Depends(client_user)):
    """Руководитель (право подписи) добавляет коллегу. Подключиться к чужой компании по ИНН нельзя."""
    m = invite_member(db, user, company_id, email=body.email, position=body.position, can_apply=body.can_apply,
                      can_sign=body.can_sign)
    db.commit()
    return s.MemberOut(user_id=m.user_id, full_name=m.user.full_name, position=m.position, can_apply=m.can_apply,
                       can_sign=m.can_sign)


# --- Заявки (клиент) ---------------------------------------------------------------

@router.get("/applications", response_model=list[s.ApplicationOut], tags=["applications"])
def list_applications(db: Session = Depends(get_db), user: User = Depends(client_user)):
    ids = [c.id for c in user_companies(db, user)]
    apps = db.scalars(select(Application).where(Application.company_id.in_(ids)).order_by(Application.id.desc())).all()
    return [app_out(db, a) for a in apps]


@router.post("/applications", response_model=s.ApplicationOut, status_code=201, tags=["applications"])
def create_application(body: s.ApplicationCreate, db: Session = Depends(get_db), user: User = Depends(client_user)):
    company = db.get(Company, body.company_id)
    product = db.get(Product, body.product_id)
    if company is None or product is None:
        raise HTTPException(404, "Компания или продукт не найдены")
    app = svc.create_draft(db, user, company, product, amount=body.amount, term_months=body.term_months,
                           goal=body.goal, has_collateral=body.has_collateral, has_guarantor=body.has_guarantor)
    db.commit()
    return app_out(db, app)


@router.get("/applications/{app_id}", response_model=s.ApplicationOut, tags=["applications"])
def get_application(app_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return app_out(db, svc.get_application(db, user, app_id))


@router.patch("/applications/{app_id}", response_model=s.ApplicationOut, tags=["applications"])
def patch_application(app_id: int, body: s.ApplicationPatch, db: Session = Depends(get_db),
                      user: User = Depends(client_user)):
    app = svc.get_application(db, user, app_id)
    svc.update_params(db, app, **body.model_dump(exclude_unset=True))
    db.commit()
    return app_out(db, app)


@router.put("/applications/{app_id}/financials", response_model=s.ApplicationOut, tags=["applications"])
def put_financials(app_id: int, body: s.FinancialsIn, db: Session = Depends(get_db),
                   user: User = Depends(client_user)):
    app = svc.get_application(db, user, app_id)
    data = body.model_dump(exclude_unset=True, exclude={"liabilities"})
    liabs = [l.model_dump() for l in body.liabilities] if body.liabilities is not None else None
    svc.update_financials(db, app, data, liabs)
    db.commit()
    return app_out(db, app)


@router.post("/applications/{app_id}/consents", response_model=s.ConsentOut, status_code=201, tags=["applications"])
def give_consent(app_id: int, body: s.ConsentIn, db: Session = Depends(get_db), user: User = Depends(client_user)):
    app = svc.get_application(db, user, app_id)
    c = svc.give_consent(db, app, user, body.kind)
    db.commit()
    return c


@router.delete("/applications/{app_id}/consents/{kind}", status_code=204, tags=["applications"])
def revoke_consent(app_id: int, kind: str, db: Session = Depends(get_db), user: User = Depends(client_user)):
    app = svc.get_application(db, user, app_id)
    svc.revoke_consent(db, app, user, kind)
    db.commit()


@router.post("/applications/{app_id}/submit", response_model=s.SubmitOut, tags=["applications"],
             responses={422: {"model": s.Error}, 409: {"model": s.Error}})
def submit(app_id: int, db: Session = Depends(get_db), user: User = Depends(client_user),
           idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    app = svc.get_application(db, user, app_id)
    res = svc.submit(db, app, user, idempotency_key)
    db.commit()
    return res


@router.post("/applications/{app_id}/withdraw", response_model=s.ApplicationOut, tags=["applications"])
def withdraw(app_id: int, db: Session = Depends(get_db), user: User = Depends(client_user)):
    app = svc.get_application(db, user, app_id)
    svc.withdraw(db, app, user)
    db.commit()
    return app_out(db, app)


@router.post("/applications/{app_id}/info-response", response_model=s.ApplicationOut, tags=["applications"])
async def info_response(app_id: int, comment: str = Form(""), files: list[UploadFile] = File(default=[]),
                        db: Session = Depends(get_db), user: User = Depends(client_user)):
    app = svc.get_application(db, user, app_id)
    payload = [(f.filename or "file", f.content_type, await f.read()) for f in files if f.filename]
    svc.respond_info(db, app, user, comment=comment, files=payload)
    db.commit()
    return app_out(db, app)


@router.post("/applications/{app_id}/offer/accept", response_model=s.ApplicationOut, tags=["applications"])
def accept(app_id: int, db: Session = Depends(get_db), user: User = Depends(client_user)):
    app = svc.get_application(db, user, app_id)
    svc.accept_offer(db, app, user)
    db.commit()
    return app_out(db, app)


@router.post("/applications/{app_id}/offer/reject", response_model=s.ApplicationOut, tags=["applications"])
def reject(app_id: int, db: Session = Depends(get_db), user: User = Depends(client_user)):
    app = svc.get_application(db, user, app_id)
    svc.reject_offer(db, app, user)
    db.commit()
    return app_out(db, app)


@router.post("/applications/{app_id}/sign", response_model=s.ApplicationOut, tags=["applications"])
def sign(app_id: int, db: Session = Depends(get_db), user: User = Depends(client_user)):
    app = svc.get_application(db, user, app_id)
    svc.sign(db, app, user)
    db.commit()
    return app_out(db, app)


@router.get("/applications/{app_id}/history", response_model=list[s.HistoryOut], tags=["applications"])
def history(app_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    svc.get_application(db, user, app_id)
    return history_rows(db, app_id)


# --- Банк -------------------------------------------------------------------------------

@router.get("/bank/queue", response_model=list[s.QueueItemOut], tags=["bank"])
def bank_queue(tab: str = "mine", q: str = "", product: str = "", db: Session = Depends(get_db),
               user: User = Depends(bank_user)):
    rows = analyst_svc.queue(db, user, tab=tab, q=q, product_code=product)
    return [s.QueueItemOut(
        application_id=r.app.id, number=r.app.number, company=r.app.company.name, inn=r.app.company.inn,
        product=r.app.product.name, amount=r.app.amount, term_months=r.app.term_months, status=r.app.status,
        score=r.score.score if r.score else None, zone=r.score.zone if r.score else None, reason=r.reason,
        assigned_to=r.task.assigned_to if r.task else None, due_at=r.task.due_at if r.task else None,
        overdue=r.overdue) for r in rows]


@router.get("/bank/applications/{app_id}", tags=["bank"])
def bank_card(app_id: int, db: Session = Depends(get_db), user: User = Depends(bank_user)):
    app = svc.get_application(db, user, app_id)
    v = application_view(db, app, bank=True)
    sc = v["score"]
    return {
        "application": app_out(db, app).model_dump(),
        "snapshot": v["snapshot"].data if v["snapshot"] else None,
        "snapshot_sha256": v["snapshot"].sha256 if v["snapshot"] else None,
        "scoring": None if sc is None else {"model_version": sc.model_version, "score": sc.score, "zone": sc.zone,
                                            "base": sc.base, "factors": sc.factors, "knockouts": sc.knockouts},
        "checks": [{"source": c.source, "endpoint": c.endpoint, "ok": c.ok, "http_status": c.http_status,
                    "attempt": c.attempt, "simulated": c.simulated, "at": c.created_at.isoformat(),
                    "response": c.response} for c in v["checks"]],
        "hint": v["hint"],
        "journal": [s.HistoryOut.model_validate(h).model_dump() for h in history_rows(db, app.id)],
    }


@router.post("/bank/applications/{app_id}/take", status_code=204, tags=["bank"])
def bank_take(app_id: int, db: Session = Depends(get_db), user: User = Depends(bank_user)):
    analyst_svc.take(db, svc.get_application(db, user, app_id), user)
    db.commit()


@router.post("/bank/applications/{app_id}/release", status_code=204, tags=["bank"])
def bank_release(app_id: int, db: Session = Depends(get_db), user: User = Depends(bank_user)):
    analyst_svc.release(db, svc.get_application(db, user, app_id), user)
    db.commit()


@router.post("/bank/applications/{app_id}/decision", response_model=s.ApplicationOut, tags=["bank"],
             responses={409: {"model": s.Error}, 422: {"model": s.Error}})
def bank_decision(app_id: int, body: s.DecisionIn, db: Session = Depends(get_db), user: User = Depends(bank_user)):
    app = svc.get_application(db, user, app_id)
    analyst_svc.decide(db, app, user, kind=body.kind, comment=body.comment, expected_version=body.expected_version,
                       amount=body.amount, term_months=body.term_months, rate=body.rate, reasons=body.reasons,
                       doc_kind=body.doc_kind, info_comment=body.info_comment, due_days=body.due_days)
    db.commit()
    return app_out(db, app)


@router.post("/bank/applications/{app_id}/client-message", response_model=s.ClientMessageOut, tags=["bank"])
def bank_client_message(app_id: int, body: s.ClientMessageIn, db: Session = Depends(get_db),
                        user: User = Depends(bank_user)):
    """Черновик пояснения для клиента по короткой заметке аналитика. Ничего не отправляет и не сохраняет."""
    svc.get_application(db, user, app_id)
    d = client_message.draft(body.note, body.doc_kind)
    return s.ClientMessageOut(text=d.text, source=d.source, rejected=d.rejected)


@router.get("/bank/metrics", tags=["bank"])
def bank_metrics(db: Session = Depends(get_db), user: User = Depends(bank_user)):
    return analyst_svc.process_metrics(db)


# --- Администрирование ---------------------------------------------------------------

@router.get("/admin/scoring-models", tags=["admin"])
def scoring_models(db: Session = Depends(get_db), user: User = Depends(bank_user)):
    rows = db.scalars(select(ScoringModel).order_by(ScoringModel.id.desc())).all()
    return [{"version": r.version, "active": r.active, "created_at": r.created_at, "config": r.config} for r in rows]


@router.post("/admin/scoring-models", status_code=201, tags=["admin"])
def new_scoring_model(body: s.ScoringModelIn, db: Session = Depends(get_db), user: User = Depends(admin_user)):
    from app.services.scoring import DEFAULT_MODEL
    missing = [k for k in DEFAULT_MODEL if k not in body.config and k != "version"]
    if missing:
        raise HTTPException(422, f"В конфигурации нет ключей: {', '.join(missing)}")
    if db.scalar(select(ScoringModel).where(ScoringModel.version == body.version)):
        raise HTTPException(409, "Такая версия уже есть")
    for r in db.scalars(select(ScoringModel).where(ScoringModel.active.is_(True))):
        r.active = False
    db.add(ScoringModel(version=body.version, config={**body.config, "version": body.version}, active=True,
                        created_by=user.id))
    db.add(AuditLog(actor_id=user.id, action="scoring.new_version", entity="scoring_model", data={"v": body.version}))
    db.commit()
    return {"version": body.version, "active": True}


@router.patch("/admin/products/{product_id}", response_model=s.ProductOut, tags=["admin"])
def patch_product(product_id: int, body: s.ProductPatch, db: Session = Depends(get_db),
                  user: User = Depends(admin_user)):
    p = db.get(Product, product_id)
    if p is None:
        raise HTTPException(404, "Продукт не найден")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(p, k, v)
    p.version += 1
    db.add(AuditLog(actor_id=user.id, action="product.update", entity="product", entity_id=p.id,
                    data={k: str(v) for k, v in body.model_dump(exclude_unset=True).items()}))
    db.commit()
    return p

