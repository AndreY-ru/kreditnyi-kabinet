"""Машина состояний заявки (раздел 5 исследования). Единственный источник правды о переходах."""
from __future__ import annotations

from enum import StrEnum


class S(StrEnum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    VALIDATING = "VALIDATING"
    NEEDS_CORRECTION = "NEEDS_CORRECTION"
    ENRICHING = "ENRICHING"
    SCORING = "SCORING"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    NEEDS_INFO = "NEEDS_INFO"
    APPROVED_AUTO = "APPROVED_AUTO"
    APPROVED = "APPROVED"
    COUNTEROFFER = "COUNTEROFFER"
    DECLINED = "DECLINED"
    OFFER_ACCEPTED = "OFFER_ACCEPTED"
    OFFER_REJECTED = "OFFER_REJECTED"
    SIGNING = "SIGNING"
    DISBURSED = "DISBURSED"
    EXPIRED = "EXPIRED"
    WITHDRAWN = "WITHDRAWN"


class Actor(StrEnum):
    SYSTEM = "SYSTEM"
    CLIENT = "CLIENT"
    ANALYST = "ANALYST"


SYS, CLI, ANL = Actor.SYSTEM, Actor.CLIENT, Actor.ANALYST

# из статуса -> {в статус: кто может выполнить переход}
TRANSITIONS: dict[S, dict[S, set[Actor]]] = {
    S.DRAFT: {S.SUBMITTED: {CLI}, S.WITHDRAWN: {CLI}},
    S.SUBMITTED: {S.VALIDATING: {SYS}},
    S.VALIDATING: {S.NEEDS_CORRECTION: {SYS}, S.ENRICHING: {SYS}},
    S.NEEDS_CORRECTION: {S.SUBMITTED: {CLI}, S.WITHDRAWN: {CLI}},
    S.ENRICHING: {S.ENRICHING: {SYS}, S.SCORING: {SYS}, S.MANUAL_REVIEW: {SYS}},
    S.SCORING: {S.APPROVED_AUTO: {SYS}, S.DECLINED: {SYS}, S.MANUAL_REVIEW: {SYS}},
    S.MANUAL_REVIEW: {S.NEEDS_INFO: {ANL}, S.APPROVED: {ANL}, S.COUNTEROFFER: {ANL}, S.DECLINED: {ANL}},
    S.NEEDS_INFO: {S.MANUAL_REVIEW: {CLI}, S.EXPIRED: {SYS}, S.WITHDRAWN: {CLI}},
    S.APPROVED_AUTO: {S.OFFER_ACCEPTED: {CLI}, S.OFFER_REJECTED: {CLI}, S.EXPIRED: {SYS}},
    S.APPROVED: {S.OFFER_ACCEPTED: {CLI}, S.OFFER_REJECTED: {CLI}, S.EXPIRED: {SYS}},
    S.COUNTEROFFER: {S.OFFER_ACCEPTED: {CLI}, S.OFFER_REJECTED: {CLI}, S.EXPIRED: {SYS}},
    S.OFFER_ACCEPTED: {S.SIGNING: {SYS}},
    S.SIGNING: {S.DISBURSED: {SYS}},
}

TERMINAL = {S.DECLINED, S.OFFER_REJECTED, S.EXPIRED, S.WITHDRAWN, S.DISBURSED}
EDITABLE = {S.DRAFT, S.NEEDS_CORRECTION}
OFFER_STATES = {S.APPROVED_AUTO, S.APPROVED, S.COUNTEROFFER}
PROCESSING = {S.SUBMITTED, S.VALIDATING, S.ENRICHING, S.SCORING}


def can_transition(frm: str, to: str, actor: Actor) -> bool:
    return actor in TRANSITIONS.get(S(frm), {}).get(S(to), set())


# --- Представление для клиента ---------------------------------------------

CLIENT_LABELS: dict[str, tuple[str, str]] = {
    # статус: (подпись, тон для бейджа)
    "DRAFT": ("Черновик", "neutral"),
    "SUBMITTED": ("Отправлена", "blue"),
    "VALIDATING": ("Проверяем данные", "blue"),
    "NEEDS_CORRECTION": ("Нужно исправить", "amber"),
    "ENRICHING": ("Запрашиваем реестры", "blue"),
    "SCORING": ("Оцениваем заявку", "blue"),
    "MANUAL_REVIEW": ("На рассмотрении", "blue"),
    "NEEDS_INFO": ("Нужны данные", "amber"),
    "APPROVED_AUTO": ("Одобрено", "green"),
    "APPROVED": ("Одобрено", "green"),
    "COUNTEROFFER": ("Есть предложение", "blue"),
    "DECLINED": ("Не одобрено", "red"),
    "OFFER_ACCEPTED": ("Предложение принято", "blue"),
    "OFFER_REJECTED": ("Вы отказались", "neutral"),
    "SIGNING": ("Подписание договора", "blue"),
    "DISBURSED": ("Выдан", "green"),
    "EXPIRED": ("Срок истёк", "neutral"),
    "WITHDRAWN": ("Отозвана", "neutral"),
}

BANK_LABELS: dict[str, tuple[str, str]] = {
    **CLIENT_LABELS,
    "MANUAL_REVIEW": ("На разборе", "blue"),
    "NEEDS_INFO": ("Ждём клиента", "amber"),
}

STAGES = ["Отправлена", "Проверка данных", "Реестры и кредитная история", "Рассмотрение", "Решение", "Договор и выдача"]

# Статус -> (индекс текущего этапа 0..5, проблема на этапе?)
_STAGE_OF: dict[str, tuple[int, bool]] = {
    "SUBMITTED": (0, False),
    "VALIDATING": (1, False),
    "NEEDS_CORRECTION": (1, True),
    "ENRICHING": (2, False),
    "SCORING": (3, False),
    "MANUAL_REVIEW": (3, False),
    "NEEDS_INFO": (3, True),
    "APPROVED_AUTO": (4, False),
    "APPROVED": (4, False),
    "COUNTEROFFER": (4, False),
    "DECLINED": (4, True),
    "OFFER_REJECTED": (4, False),
    "OFFER_ACCEPTED": (5, False),
    "SIGNING": (5, False),
    "DISBURSED": (6, False),
}


def stage_of(status: str) -> tuple[int, bool]:
    return _STAGE_OF.get(status, (-1, False))
