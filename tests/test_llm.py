"""LLM: пересказ объяснения в фоне, проверка ответа модели, решение от модели не зависит."""
from __future__ import annotations

import httpx
import pytest

from app.config import settings
from app.services import explain
from tests.helpers import OVERDUE, consent_all, draft, process


class _FakeResp:
    def __init__(self, text: str):
        self._text = text

    def raise_for_status(self):
        return None

    def json(self):
        return {"response": self._text}


@pytest.fixture
def llm_on(monkeypatch):
    monkeypatch.setattr(settings, "llm_url", "http://llm.test")
    calls = []

    def use(text: str | None):
        def fake_post(url, json, timeout):
            calls.append(json["prompt"])
            if text is None:
                raise httpx.ConnectError("нет модели")
            return _FakeResp(text)
        monkeypatch.setattr(explain.httpx, "post", fake_post)
        return calls
    return use


def _declined_app(anna) -> int:
    fin = {"revenue": 18_000_000, "expenses": 16_900_000, "ebit": 1_500_000, "amortization": 200_000,
           "revenue_trend": "FLAT", "liabilities": []}
    app = draft(anna, OVERDUE, "WORKING", 1_000_000, 12, fin=fin)
    consent_all(anna, app["id"])
    anna.post(f"/api/applications/{app['id']}/submit")
    return app["id"]


def _decision(db, app_id):
    from app.models import Decision

    db.expire_all()
    return db.query(Decision).filter_by(application_id=app_id, outcome="DECLINED").one()


def test_llm_rephrase_runs_in_background_and_is_kept_when_safe(anna, db, llm_on):
    calls = llm_on("Банк пока не может выдать этот кредит: есть просрочка по другому кредиту. "
                   "Закройте её и подайте заявку снова.")
    app_id = _declined_app(anna)
    process()
    d = _decision(db, app_id)
    assert d.explanation_source == "LLM" and "просрочка" in d.explanation
    assert len(calls) == 1 and "Не добавляй новых фактов" in calls[0]


def test_llm_output_with_invented_numbers_is_rejected(anna, db, llm_on):
    llm_on("Вам одобрят 500 000 рублей под 12% годовых, если подадите через месяц.")
    app_id = _declined_app(anna)
    process()
    d = _decision(db, app_id)
    assert d.explanation_source == "TEMPLATE"


def test_llm_down_does_not_block_decision(anna, db, llm_on):
    from app.models import Job

    llm_on(None)
    app_id = _declined_app(anna)
    process()
    assert anna.get(f"/api/applications/{app_id}").json()["status"] == "DECLINED"
    assert _decision(db, app_id).explanation_source == "TEMPLATE"
    job = db.query(Job).filter_by(kind=explain.REPHRASE_JOB).one()
    assert job.status == "PENDING" and job.attempts == 1  # повторит позже, решение уже у клиента


def test_safe_rephrase_rules():
    src = "Сейчас банк не может одобрить «Оборотный кредит». Главное, что повлияло: платёжная история."
    assert explain.is_safe_rephrase("Пока кредит не одобрен из-за просрочек по другим долгам. Погасите их.", src)
    assert not explain.is_safe_rephrase("Одобрим 3 млн после погашения.", src)
    assert not explain.is_safe_rephrase("Мы гарантируем одобрение в следующий раз, просто подождите.", src)
    assert not explain.is_safe_rephrase("The bank cannot approve your loan due to payment history.", src)
    assert not explain.is_safe_rephrase("Нет.", src)


def test_no_llm_job_when_llm_disabled(anna, db):
    from app.models import Job

    _declined_app(anna)
    process()
    assert db.query(Job).filter_by(kind=explain.REPHRASE_JOB).count() == 0
