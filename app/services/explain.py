"""Объяснимость: категории причин для клиента без раскрытия порогов и баллов (раздел 6.4).

Тексты причин и советов написаны заранее и проверены — их не пересказывает модель: она могла бы
только исказить решение. ИИ в проекте помогает там, где без него хуже: разобрать запрос клиента
своими словами (app/services/intake.py) и сформулировать сообщение клиенту для аналитика
(app/services/client_message.py).
"""
from __future__ import annotations

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


def explain(outcome: str, codes: list[str] | None, product_name: str) -> tuple[str, str]:
    return template_explanation(outcome, codes, product_name), "TEMPLATE"
