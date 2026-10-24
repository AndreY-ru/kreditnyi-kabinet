"""Объяснимость: категории причин для клиента без раскрытия порогов и баллов (раздел 6.4).

Основной текст детерминированный и сохраняется в решении сразу. LLM с открытыми весами (через Ollama)
только пересказывает его простым языком — фоновой задачей в outbox (REPHRASE_EXPLANATION), поэтому
медленная модель не задерживает ни конвейер, ни аналитика. Ответ модели проверяется: новые числа,
обещания или не русский текст — отбрасываем и оставляем шаблон. Основной сценарий от модели не зависит.
"""
from __future__ import annotations

import logging
import re

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Decision, Job

log = logging.getLogger(__name__)

REPHRASE_JOB = "REPHRASE_EXPLANATION"
REPHRASE_OUTCOMES = ("COUNTEROFFER", "DECLINED")


class LLMUnavailable(Exception):
    """Модель не ответила — задачу можно повторить позже."""

REASONS: dict[str, dict[str, str]] = {
    "BUSINESS_AGE": {
        "title": "Срок работы бизнеса",
        "text": "Компания работает меньше, чем требуется для этого продукта.",
        "action": "Выберите продукт с меньшим требованием к сроку работы или подайте заявку позже.",
    },
    "DEBT_LOAD": {
        "title": "Долговая нагрузка",
        "text": "С учётом действующих кредитов и лизинга платежи по запрошенной сумме были бы высокими "
                "относительно прибыли компании.",
        "action": "Уменьшите сумму, увеличьте срок или погасите часть действующих обязательств.",
    },
    "TAX_ARREARS": {
        "title": "Налоговая задолженность",
        "text": "По данным налоговой у компании есть недоимка.",
        "action": "Погасите задолженность и обновите заявку — сведения в реестре обновляются в течение нескольких дней.",
    },
    "PAYMENT_HISTORY": {
        "title": "Платёжная история",
        "text": "В кредитной истории есть просрочки по действующим обязательствам.",
        "action": "Погасите текущую просрочку. После нескольких месяцев платежей без просрочек шансы вырастут.",
    },
    "COLLATERAL": {
        "title": "Обеспечение",
        "text": "Для этой суммы нужен залог, а в заявке его нет.",
        "action": "Предложите залог (например, приобретаемое оборудование) или уменьшите сумму.",
    },
    "FINANCIALS": {
        "title": "Финансовые показатели",
        "text": "Текущие финансовые показатели компании не позволяют одобрить заявку на этих условиях.",
        "action": "Попробуйте меньшую сумму или вернитесь после следующей отчётности.",
    },
    "REQUEST_SIZE": {
        "title": "Размер запроса",
        "text": "Запрошенная сумма велика относительно годовой выручки компании.",
        "action": "Уменьшите сумму или разделите финансирование на этапы.",
    },
    "COMPANY_STATUS": {
        "title": "Статус компании",
        "text": "По данным реестра компания находится в процессе ликвидации или реорганизации.",
        "action": "Если сведения неверны, обновите их в налоговой.",
    },
}

POSITIVE: dict[str, str] = {
    "business_age": "Срок работы компании",
    "payment_discipline": "Платёжная история без замечаний",
    "revenue_trend": "Рост выручки",
    "guarantor": "Поручительство собственника",
    "collateral": "Залог",
}


def reasons_for(codes: list[str] | None) -> list[dict[str, str]]:
    return [{"code": c, **REASONS[c]} for c in (codes or []) if c in REASONS]


def positives_for(factors: list[dict] | None) -> list[str]:
    return [POSITIVE[f["code"]] for f in (factors or []) if f["points"] > 0 and f["code"] in POSITIVE]


def template_explanation(outcome: str, codes: list[str] | None, product_name: str) -> str:
    rs = reasons_for(codes)
    if outcome in ("APPROVED_AUTO", "APPROVED"):
        return f"Заявка на «{product_name}» одобрена на запрошенных условиях."
    if outcome == "COUNTEROFFER":
        base = f"Банк готов выдать «{product_name}» на других условиях."
    elif outcome == "DECLINED":
        base = f"Сейчас банк не может одобрить «{product_name}»."
    else:
        return ""
    if rs:
        base += " Главное, что повлияло: " + "; ".join(r["title"].lower() for r in rs) + "."
        base += " Что можно сделать: " + " ".join(r["action"] for r in rs)
    return base


def llm_rephrase(text: str) -> str | None:
    """Пересказ через Ollama. None — модель выключена; LLMUnavailable — не ответила (повторим)."""
    if not settings.llm_url or not text:
        return None
    prompt = (
        "Перескажи для предпринимателя без банковских терминов, 2-3 предложения, на русском. "
        "Не добавляй новых фактов, чисел, порогов и обещаний. Текст:\n" + text
    )
    try:
        resp = httpx.post(
            f"{settings.llm_url.rstrip('/')}/api/generate",
            json={"model": settings.llm_model, "prompt": prompt, "stream": False,
                  "options": {"temperature": 0.2, "num_predict": 200}},
            timeout=settings.llm_timeout,
        )
        resp.raise_for_status()
        return (resp.json().get("response") or "").strip() or None
    except (httpx.HTTPError, ValueError) as exc:
        raise LLMUnavailable(str(exc)) from exc


_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_PROMISES = ("гарантир", "точно одобр", "обязательно одобр")


def is_safe_rephrase(candidate: str, source: str) -> bool:
    """Отсекаем пересказ, который мог исказить решение: новые числа, обещания, не тот язык, не та длина."""
    text = candidate.strip()
    if not 20 <= len(text) <= 700:
        return False
    letters = [ch.lower() for ch in text if ch.isalpha()]
    if not letters or sum("а" <= ch <= "я" or ch == "ё" for ch in letters) / len(letters) < 0.8:
        return False
    if set(_NUMBER.findall(text)) - set(_NUMBER.findall(source)):
        return False
    low = text.lower()
    return not any(p in low for p in _PROMISES)


def explain(outcome: str, codes: list[str] | None, product_name: str) -> tuple[str, str]:
    """Детерминированное объяснение. Пересказ моделью, если включён, — позже, см. enqueue_rephrase."""
    return template_explanation(outcome, codes, product_name), "TEMPLATE"


def enqueue_rephrase(db: Session, app_id: int, outcome: str) -> None:
    if settings.llm_url and outcome in REPHRASE_OUTCOMES:
        db.add(Job(kind=REPHRASE_JOB, application_id=app_id))


def rephrase_decision(db: Session, app_id: int) -> bool:
    """Задача воркера: заменить шаблонное объяснение последнего решения пересказом модели, если он безопасен."""
    d = db.scalar(select(Decision).where(Decision.application_id == app_id, Decision.outcome.in_(REPHRASE_OUTCOMES))
                  .order_by(Decision.id.desc()))
    if d is None or d.explanation_source != "TEMPLATE" or not d.explanation:
        return False
    out = llm_rephrase(d.explanation)
    if not out or not is_safe_rephrase(out, d.explanation):
        log.info("Пересказ для заявки %s отклонён, остаётся шаблон", app_id)
        return False
    d.explanation, d.explanation_source = out, "LLM"
    db.commit()
    return True
