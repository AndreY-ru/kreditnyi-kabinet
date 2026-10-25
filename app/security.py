"""Аутентификация (сессия в подписанной cookie), хеширование паролей, RBAC, CSRF."""
from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import ratelimit
from app.db import get_db
from app.models import AuditLog, User

ITERATIONS = 240_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, digest = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iters))
    return hmac.compare_digest(dk.hex(), digest)


class LoginRequired(Exception):
    pass


class TooManyAttempts(Exception):
    def __init__(self, retry_after: int):
        super().__init__(ratelimit.wait_text(retry_after))
        self.retry_after = retry_after


_DUMMY_HASH = hash_password("timing-equalizer")


def authenticate(db: Session, email: str, password: str, ip: str | None) -> User | None:
    """Проверка входа для web и API одна: лимит попыток, активность, одинаковое время для любого email."""
    email = email.strip().lower()
    wait = ratelimit.login_blocked(ip, email)
    if wait:
        raise TooManyAttempts(wait)
    user = db.scalar(select(User).where(User.email == email))
    ok = verify_password(password, user.password_hash if user else _DUMMY_HASH)  # без утечки «email существует»
    if user is None or not user.is_active or not ok:
        ratelimit.login_failed(ip, email)
        db.add(AuditLog(actor_id=user.id if user else None, action="auth.login_failed", entity="user",
                        entity_id=user.id if user else None, data={"email": email[:255]}))
        db.commit()
        return None
    ratelimit.login_succeeded(ip, email)
    return user


def start_session(request: Request, db: Session, user: User) -> None:
    request.session.clear()  # новая сессия при входе — защита от фиксации сессии
    request.session["uid"] = user.id
    db.add(AuditLog(actor_id=user.id, action="auth.login", entity="user", entity_id=user.id))
    db.commit()


def current_user_optional(request: Request, db: Session = Depends(get_db)) -> User | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    user = db.get(User, uid)
    if user is None or not user.is_active:
        request.session.clear()
        return None
    return user


def current_user(request: Request, user: User | None = Depends(current_user_optional)) -> User:
    if user is None:
        if request.url.path.startswith("/api/"):
            raise HTTPException(status_code=401, detail="Требуется вход")
        raise LoginRequired()
    return user


def require_role(*roles: str):
    def dep(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="Недостаточно прав")
        return user

    return dep


client_user = require_role("CLIENT")
bank_user = require_role("ANALYST", "ADMIN")
admin_user = require_role("ADMIN")


# --- CSRF для HTML-форм -------------------------------------------------------

def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        request.session["csrf"] = tok
    return tok


async def verify_csrf(request: Request) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    expected = request.session.get("csrf")
    sent = request.headers.get("X-CSRF-Token")
    if not sent:
        form = await request.form()
        sent = form.get("csrf_token")  # type: ignore[assignment]
    if not expected or not sent or not hmac.compare_digest(str(sent), expected):
        raise HTTPException(status_code=403, detail="Сессия устарела. Обновите страницу.")
