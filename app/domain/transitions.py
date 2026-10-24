"""Сервис переходов: единственное место, где меняется Application.status.

Недопустимый переход отклоняется здесь, а не в интерфейсе. Каждый переход пишется в журнал.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.domain.states import Actor, can_transition
from app.models import Application, StatusHistory, User, utcnow


class DomainError(Exception):
    status_code = 400

    def __init__(self, message: str, *, code: str = "domain_error", details: object | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details


class InvalidTransition(DomainError):
    status_code = 409


class VersionConflict(DomainError):
    status_code = 409


class NotFound(DomainError):
    status_code = 404


class Forbidden(DomainError):
    status_code = 403


class ValidationFailed(DomainError):
    status_code = 422


@dataclass(frozen=True)
class ActorRef:
    kind: Actor
    user_id: int | None = None
    name: str = "система"

    @classmethod
    def system(cls) -> ActorRef:
        return cls(Actor.SYSTEM)

    @classmethod
    def of(cls, user: User, kind: Actor) -> ActorRef:
        return cls(kind, user.id, user.full_name)


def transition(
    db: Session,
    app: Application,
    to: str,
    *,
    event: str,
    actor: ActorRef,
    reason: str | None = None,
    expected_version: int | None = None,
) -> StatusHistory:
    if expected_version is not None and app.version != expected_version:
        raise VersionConflict(
            "Заявку уже изменил другой пользователь. Обновите страницу.", code="version_conflict"
        )
    frm = app.status
    if not can_transition(frm, to, actor.kind):
        raise InvalidTransition(
            f"Переход {frm} → {to} недопустим для {actor.kind.value}", code="invalid_transition"
        )
    hist = StatusHistory(
        application_id=app.id,
        from_status=frm,
        to_status=to,
        event=event,
        actor_type=actor.kind.value,
        actor_id=actor.user_id,
        actor_name=actor.name,
        reason=reason,
        created_at=utcnow(),
    )
    app.status = to
    app.updated_at = utcnow()
    db.add(hist)
    try:
        db.flush()  # version_id_col проверит версию строки в БД
    except StaleDataError as exc:
        db.rollback()
        raise VersionConflict(
            "Заявку уже изменил другой пользователь. Обновите страницу.", code="version_conflict"
        ) from exc
    return hist
