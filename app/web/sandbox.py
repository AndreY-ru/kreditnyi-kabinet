"""Страница «Тестовые данные» администратора банка: кредитная история в БКИ и сведения ФНС (только демо).

Адресуем компании по ИНН: в источниках есть и те, что ещё не подключены к кабинету (их подключает клиент).
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.domain.transitions import NotFound
from app.models import Application, AuditLog, Company, User
from app.security import admin_user, verify_csrf
from app.services import integrations
from app.services import sandbox as sb
from app.services.companies import apply_egrul, inn_is_valid
from app.services.pipeline import active_model
from app.web.common import flash, page

router = APIRouter(prefix="/bank/sandbox", include_in_schema=False, dependencies=[Depends(verify_csrf)])


def _check_inn(inn: str) -> str:
    if not inn_is_valid(inn):  # только цифры с верной контрольной суммой — ничего лишнего в адрес мока
        raise NotFound("Компания не найдена")
    return inn


def _companies(db: Session) -> list[dict[str, Any]]:
    """Все компании источников и кабинета: сначала подключённые (в порядке демо-данных), потом остальные."""
    connected = {c.inn: c for c in db.scalars(select(Company).order_by(Company.id))}
    status, rows = integrations.fns_companies()
    mock = {r["inn"]: r for r in rows} if status == 200 and isinstance(rows, list) else {}
    out = [{"inn": inn, "name": c.name, "connected": True, "custom": mock.get(inn, {}).get("custom", False)}
           for inn, c in connected.items()]
    out += [{"inn": r["inn"], "name": r["name"], "connected": False, "custom": r["custom"]}
            for r in mock.values() if r["inn"] not in connected]
    return out


def _ctx(request: Request, db: Session, user: User, inn: str | None, *, loans: list | None = None,
         errors: list[str] | None = None, new_company: dict | None = None) -> dict[str, Any]:
    companies = _companies(db)
    current = next((c for c in companies if c["inn"] == inn), None) or (companies[0] if companies else None)
    ctx: dict[str, Any] = {"user": user, "nav": "sandbox", "companies": companies, "current": current,
                           "kinds": sb.KINDS, "statuses": sb.STATUSES, "presets": sb.PRESETS, "errors": errors or [],
                           "new_company": new_company or {"kind": "OOO", "registered_at":
                                                          (date.today() - timedelta(days=3 * 365)).isoformat()}}
    if current is None:
        return ctx
    st_b, bki = integrations.bki_admin(current["inn"])
    st_f, fns = integrations.fns_admin(current["inn"])
    ctx["bki_error"] = None if st_b == 200 else f"Мок БКИ не ответил ({st_b or 'нет связи'})"
    ctx["fns_error"] = None if st_f == 200 else f"Мок ФНС не ответил ({st_f or 'нет связи'})"
    ctx["bki"] = bki if st_b == 200 else {"loans": [], "overridden": False}
    if loans is not None:  # после ошибки показываем то, что ввели, а не сохранённое
        ctx["bki"] = {**ctx["bki"], "loans": loans}
    ctx["fns"] = fns if st_f == 200 else {}
    ctx["preview"] = sb.preview(ctx["bki"]["loans"], active_model(db))
    company = db.scalar(select(Company).where(Company.inn == current["inn"]))
    ctx["app_count"] = db.scalar(select(func.count()).select_from(Application).where(
        Application.company_id == company.id, Application.submitted_at.is_not(None))) if company else None
    return ctx


def _audit(db: Session, user: User, action: str, inn: str, what: str) -> None:
    company = db.scalar(select(Company).where(Company.inn == inn))
    db.add(AuditLog(actor_id=user.id, action=action, entity="company", entity_id=company.id if company else None,
                    data={"inn": inn, "action": what}))
    db.commit()


@router.get("", response_class=HTMLResponse)
def sandbox(request: Request, inn: str | None = None, db: Session = Depends(get_db), user: User = Depends(admin_user)):
    return page(request, "bank/sandbox.html", _ctx(request, db, user, inn))


@router.post("/companies")
async def create_company(request: Request, db: Session = Depends(get_db), user: User = Depends(admin_user)):
    form = await request.form()
    data, errors = sb.parse_new_company(form)
    if not errors:
        status, body = integrations.fns_company_create(data)
        if status == 201:
            _audit(db, user, "sandbox.company", body["inn"], "создана")
            flash(request, f"Компания {body['name']} создана, ИНН {body['inn']}. Клиент может подключить её: "
                           "«Компания» → «Добавить по ИНН».")
            return RedirectResponse(f"/bank/sandbox?inn={body['inn']}", 303)
        detail = body.get("detail") if isinstance(body, dict) else None
        errors = [detail if status in (409, 422) and isinstance(detail, str)
                  else f"Мок ФНС не принял компанию ({status or 'нет связи'})"]
    return page(request, "bank/sandbox.html", _ctx(request, db, user, None, errors=errors, new_company=dict(form)),
                status_code=422)


@router.post("/{inn}/remove")
def remove_company(request: Request, inn: str, db: Session = Depends(get_db), user: User = Depends(admin_user)):
    inn = _check_inn(inn)
    if db.scalar(select(Company.id).where(Company.inn == inn)) is not None:
        flash(request, "Компания уже подключена к кабинету — её нельзя удалить, но можно поменять данные", "err")
        return RedirectResponse(f"/bank/sandbox?inn={inn}", 303)
    status, _ = integrations.fns_company_remove(inn)
    if status != 200:
        flash(request, "Удалить можно только компанию, добавленную на этой странице", "err")
        return RedirectResponse(f"/bank/sandbox?inn={inn}", 303)
    _audit(db, user, "sandbox.company", inn, "удалена")
    flash(request, f"Компания с ИНН {inn} удалена из источников")
    return RedirectResponse("/bank/sandbox", 303)


@router.post("/{inn}/bki")
async def save_bki(request: Request, inn: str, db: Session = Depends(get_db), user: User = Depends(admin_user)):
    inn = _check_inn(inn)
    form = await request.form()
    action = str(form.get("action") or "save")
    if action == "reset":
        status, _ = integrations.bki_admin(inn, "DELETE")
        done = "Кредитная история — снова исходная"
    elif action.startswith("preset:") and action[7:] in sb.PRESETS:
        _, current = integrations.bki_admin(inn)
        status, _ = integrations.bki_admin(inn, "PUT", sb.apply_preset(current.get("loans", []), action[7:]))
        done = f"Сценарий «{sb.PRESETS[action[7:]]}» применён"
    else:
        loans, errors = sb.parse_loans(form)
        if errors:
            return page(request, "bank/sandbox.html", _ctx(request, db, user, inn, loans=loans, errors=errors),
                        status_code=422)
        status, _ = integrations.bki_admin(inn, "PUT", loans)
        done = "Кредитная история сохранена"
    if status != 200:
        flash(request, f"Мок БКИ не принял изменения ({status or 'нет связи'})", "err")
    else:
        _audit(db, user, "sandbox.bki", inn, action)
        flash(request, f"{done}. Её получат новые заявки компании; рассмотренные хранят прежний отчёт.")
    return RedirectResponse(f"/bank/sandbox?inn={inn}#bki", 303)


@router.post("/{inn}/fns")
async def save_fns(request: Request, inn: str, db: Session = Depends(get_db), user: User = Depends(admin_user)):
    inn = _check_inn(inn)
    form = await request.form()
    if form.get("action") == "reset":
        status, _ = integrations.fns_admin(inn, "DELETE")
    else:
        fields, errors = sb.parse_fns(form)
        if errors:
            return page(request, "bank/sandbox.html", _ctx(request, db, user, inn, errors=errors), status_code=422)
        status, _ = integrations.fns_admin(inn, "PUT", fields)
    if status != 200:
        flash(request, f"Мок ФНС не принял изменения ({status or 'нет связи'})", "err")
        return RedirectResponse(f"/bank/sandbox?inn={inn}#fns", 303)
    # Подключённая компания сразу видит новый статус и дату регистрации — как после запроса в ЕГРЮЛ
    company = db.scalar(select(Company).where(Company.inn == inn))
    res = integrations.fns_egrul(inn) if company else None
    if company and res and res.ok and res.response:
        apply_egrul(company, res.response)
    _audit(db, user, "sandbox.fns", inn, str(form.get("action") or "save"))
    flash(request, "Сведения ФНС сохранены. Их увидят «Проверка шансов» и новые заявки.")
    return RedirectResponse(f"/bank/sandbox?inn={inn}#fns", 303)
