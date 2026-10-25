"""Общие HTML-маршруты: вход, выход, корень, скачивание документов. Плюс помощники для шаблонов."""
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.domain.transitions import NotFound
from app.models import Company, Document, User
from app.security import (
    TooManyAttempts,
    authenticate,
    csrf_token,
    current_user,
    current_user_optional,
    start_session,
    verify_csrf,
)
from app.services.applications import get_application
from app.services.companies import user_companies
from app.templating import templates

router = APIRouter(include_in_schema=False)

DEMO_USERS = [
    ("anna@demo.local", "Анна Петрова", "Клиент · финдиректор, несколько компаний"),
    ("igor@demo.local", "Игорь Смирнов", "Клиент · гендиректор, подписывает договоры"),
    ("elena@demo.local", "Елена Козлова", "Клиент · ИП"),
    ("maria@bank.local", "Мария Котова", "Банк · кредитный аналитик"),
    ("petr@bank.local", "Пётр Волков", "Банк · второй аналитик"),
    ("admin@bank.local", "Админ", "Банк · продукты и скоринг"),
]


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append({"m": message, "k": kind})


def pop_flash(request: Request) -> list[dict[str, str]]:
    return request.session.pop("flash", [])


def active_company(request: Request, db: Session, user: User) -> tuple[Company | None, list[Company]]:
    companies = user_companies(db, user)
    cid = request.session.get("company_id")
    current = next((c for c in companies if c.id == cid), None) or (companies[0] if companies else None)
    if current and request.session.get("company_id") != current.id:
        request.session["company_id"] = current.id
    return current, companies


def page(request: Request, name: str, ctx: dict[str, Any], status_code: int = 200) -> HTMLResponse:
    base = {"csrf": csrf_token(request), "flashes": pop_flash(request)}
    return templates.TemplateResponse(request, name, {**base, **ctx}, status_code=status_code)


def partial(request: Request, name: str, ctx: dict[str, Any]) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"csrf": csrf_token(request), **ctx})


@router.get("/")
def root(user: User | None = Depends(current_user_optional)):
    if user is None:
        return RedirectResponse("/login", 303)
    return RedirectResponse("/cabinet" if user.role == "CLIENT" else "/bank/queue", 303)


@router.get("/login")
def login_page(request: Request, next: str = "", user: User | None = Depends(current_user_optional)):
    if user is not None:
        return RedirectResponse("/", 303)
    return page(request, "login.html", {"demo_users": DEMO_USERS, "next": next, "error": None, "email": ""})


@router.post("/login", dependencies=[Depends(verify_csrf)])
def login_submit(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form(""),
                 db: Session = Depends(get_db)):
    ctx = {"demo_users": DEMO_USERS, "next": next, "email": email}
    try:
        user = authenticate(db, email, password, request.client.host if request.client else None)
    except TooManyAttempts as e:
        return page(request, "login.html", {**ctx, "error": str(e)}, status_code=429)
    if user is None:
        return page(request, "login.html", {**ctx, "error": "Неверный email или пароль"}, status_code=401)
    start_session(request, db, user)
    return RedirectResponse(safe_next(next), 303)


def safe_next(target: str) -> str:
    """Только относительный путь внутри сайта. «/\\evil.com» браузер понимает как «//evil.com»."""
    if target.startswith("/") and not target.startswith("//") and "\\" not in target and "\n" not in target:
        return target
    return "/"


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)


@router.get("/documents/{doc_id}")
def download(doc_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    doc = db.get(Document, doc_id)
    if doc is None:
        raise NotFound("Документ не найден")
    get_application(db, user, doc.application_id)  # RBAC
    if not os.path.exists(doc.storage_path):
        raise NotFound("Файл не найден в хранилище")
    return FileResponse(doc.storage_path, filename=doc.filename, media_type=doc.content_type or None)
