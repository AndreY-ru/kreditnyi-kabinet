"""Учётные записи клиентов: регистрация и смена пароля.

Зарегистрироваться может только представитель бизнеса (роль CLIENT); сотрудников банка заводит банк
(в планах — вход через корпоративный SSO). Дальше клиент подключает компанию по ИНН (services/companies.py):
первый подключивший — администратор с правом подписи, коллег добавляет он.

Подтверждения email или телефона в MVP нет: по условию кейса сценарий не уходит в почту и SMS.
В реальном банке здесь вход через Госуслуги или ЭП и подтверждение телефона — это в «Потенциале развития».
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import ratelimit
from app.domain.transitions import ValidationFailed
from app.models import AuditLog, User
from app.security import TooManyAttempts, hash_password, verify_password

MIN_PASSWORD = 8
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_WEAK = {"password", "password1", "12345678", "123456789", "1234567890", "qwertyui", "qwerty123", "11111111",
         "00000000", "йцукенгш", "demodemo", "kabinet1"}
PD_CONSENT_VERSION = "v1"


def password_problems(password: str, email: str = "") -> list[str]:
    problems = []
    if len(password) < MIN_PASSWORD:
        problems.append(f"Пароль — не короче {MIN_PASSWORD} символов")
    if password.lower() in _WEAK or (email and password.lower() == email.lower()):
        problems.append("Слишком простой пароль — его легко подобрать")
    if password.isdigit() or password.isalpha():
        problems.append("Добавьте в пароль и буквы, и цифры")
    return problems


def register(db: Session, *, full_name: str, email: str, password: str, pd_consent: bool,
             ip: str | None) -> User:
    wait = ratelimit.registrations.retry_after(str(ip))
    if wait:
        raise TooManyAttempts(wait)
    email, full_name = email.strip().lower(), " ".join(full_name.split())
    errors: dict[str, str] = {}
    if not 2 <= len(full_name) <= 255:
        errors["full_name"] = "Укажите фамилию и имя"
    if not _EMAIL.match(email) or len(email) > 255:
        errors["email"] = "Проверьте email"
    if problems := password_problems(password, email):
        errors["password"] = problems[0]
    if not pd_consent:
        errors["pd_consent"] = "Без согласия на обработку персональных данных зарегистрироваться нельзя"
    if not errors and db.scalar(select(User.id).where(User.email == email)) is not None:
        errors["email"] = "Этот email уже зарегистрирован — войдите"
    if errors:
        raise ValidationFailed("Проверьте данные регистрации", code="registration_invalid", details=errors)
    user = User(email=email, full_name=full_name, role="CLIENT", password_hash=hash_password(password))
    db.add(user)
    try:
        db.flush()
    except IntegrityError:  # тот же email зарегистрировали параллельно
        db.rollback()
        raise ValidationFailed("Проверьте данные регистрации", code="registration_invalid",
                               details={"email": "Этот email уже зарегистрирован — войдите"}) from None
    db.add(AuditLog(actor_id=user.id, action="user.register", entity="user", entity_id=user.id,
                    data={"pd_consent": True, "pd_consent_version": PD_CONSENT_VERSION}))
    ratelimit.registrations.hit(str(ip))
    return user


def change_password(db: Session, user: User, *, current: str, new: str) -> None:
    errors: dict[str, str] = {}
    if not verify_password(current, user.password_hash):
        errors["current"] = "Текущий пароль указан неверно"
    elif problems := password_problems(new, user.email):
        errors["new"] = problems[0]
    elif current == new:
        errors["new"] = "Новый пароль совпадает с текущим"
    if errors:
        raise ValidationFailed("Пароль не изменён", code="password_invalid", details=errors)
    user.password_hash = hash_password(new)
    db.add(AuditLog(actor_id=user.id, action="user.password_change", entity="user", entity_id=user.id))
