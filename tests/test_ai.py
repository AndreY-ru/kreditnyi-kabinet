"""ИИ-помощник: подбор по описанию своими словами.

Модель в тестах подменяется: проверяем, что её ответы сверяются с текстом и правилами, а без модели
всё работает на правилах и шаблонах.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.config import settings
from app.services import intake, llm
from tests.test_web import web_login

SEASON = "Нужно 3 млн на закупку товара к сезону, отдам за полгода, есть грузовик в залог"


class _Resp:
    def __init__(self, content: str):
        self._content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"message": {"content": self._content}}


@pytest.fixture
def model(monkeypatch):
    """model(ответ) — включить подставную модель; ответ None — модель недоступна."""
    monkeypatch.setattr(settings, "llm_url", "http://llm.test")
    monkeypatch.setattr(llm, "_down_until", 0.0)  # предохранитель из прошлого теста не мешает
    calls: list[dict] = []

    def use(answer):
        def fake_post(url, json, timeout):  # noqa: A002 — как у httpx
            calls.append(json)
            if answer is None:
                raise httpx.ConnectError("нет модели")
            return _Resp(answer if isinstance(answer, str) else globals()["json"].dumps(answer))
        monkeypatch.setattr(llm.httpx, "post", fake_post)
        return calls
    return use


# --- Подбор своими словами: правила ----------------------------------------------------------

@pytest.mark.parametrize("text, goal, amount, term, collateral", [
    (SEASON, "working", 3_000_000, 6, True),
    ("Выручка 50 млн в год, работаем 5 лет. Хочу взять 2,5 млн на новый станок на 2 года",
     "equipment", 2_500_000, 24, None),
    ("кассовый разрыв, покупатели задерживают оплату, надо 800к на 3 месяца, без залога",
     "cash_gap", 800_000, 3, False),
    ("хотим открыть вторую кофейню, нужно полтора миллиона, вернём за полтора года",
     "new_direction", 1_500_000, 18, None),
    ("Рефинансировать кредит в другом банке, остаток 4 200 000, срок 36 месяцев, ставка 20 годовых",
     "refinance", 4_200_000, 36, None),
])
def test_rules_understand_typical_requests(text, goal, amount, term, collateral):
    r = intake.parse_rules(text)
    assert (r.goal, r.amount, r.term, r.collateral) == (goal, amount, term, collateral)


def test_rules_do_not_invent_missing_numbers():
    r = intake.parse_rules("нужны деньги на оборудование")
    assert r.goal == "equipment" and r.amount is None and r.term is None
    assert r.missing == ["сумму", "срок"]


# --- Подбор своими словами: модель -------------------------------------------------------------

def test_model_picks_requested_amount_from_text(model):
    calls = model({"goal": "equipment", "amount_rub": 2_500_000, "term_months": 24, "collateral": "unknown",
                   "guarantor": "yes"})
    r = intake.parse("Выручка 50 млн, нужно 2,5 млн на станок на 2 года, собственник поручится")
    assert (r.source, r.goal, r.amount, r.term, r.guarantor) == ("llm", "equipment", 2_500_000, 24, True)
    assert calls[0]["format"]["required"]  # строгий формат ответа
    assert calls[0]["think"] is False


def test_model_cannot_invent_amount_or_term(model):
    model({"goal": "working", "amount_rub": 7_000_000, "term_months": 60, "collateral": "yes", "guarantor": "yes"})
    r = intake.parse(SEASON)
    assert r.amount == 3_000_000 and r.term == 6  # 7 млн и 60 мес. в тексте нет — берём из текста
    assert r.notes and "нет в тексте" in r.notes[0]


def test_model_down_falls_back_to_rules(model):
    model(None)
    r = intake.parse(SEASON)
    assert r.source == "rules" and r.amount == 3_000_000


def test_model_down_is_not_retried_on_every_request(model):
    calls = model(None)
    intake.parse(SEASON)
    intake.parse(SEASON)
    assert len(calls) == 1  # после сбоя полминуты не ждём таймаут на каждом запросе


def test_model_garbage_falls_back_to_rules(model):
    model("Конечно! Вот ваш ответ: сумма три миллиона")
    assert intake.parse(SEASON).source == "rules"


def test_describe_fills_podbor_form_and_shows_how_understood():
    c = web_login("anna@demo.local")
    r = c.post("/podbor/describe", data={"text": SEASON})
    assert r.status_code == 200 and "/podbor?" in str(r.url)
    assert "Поняли так" in r.text and "3 000 000 ₽" in r.text and "есть залог" in r.text
    assert 'value="working" checked' in r.text and 'id="t-6" name="term" value="6" checked' in r.text
    assert "Подходит под вашу задачу" in r.text  # продукты уже подобраны


def test_parse_api(anna):
    r = anna.post("/api/podbor/parse", json={"text": SEASON})
    assert r.status_code == 200
    assert r.json() | {"summary": ""} == {"goal": "working", "amount": 3_000_000, "term_months": 6,
                                          "has_collateral": True, "has_guarantor": None, "source": "rules",
                                          "missing": [], "summary": ""}


def test_safe_text_rules():
    src = "нужна ОСВ 62"
    assert llm.safe_text("Пожалуйста, приложите ведомость по счёту 62, она поможет нам.", source=src) is None
    assert llm.safe_text("Одобрим сразу после ведомости по счёту 62, обещаем.", source=src) is not None
    assert llm.numbers("1 000 000 и 2,5") == {"1000000", "2.5"}
