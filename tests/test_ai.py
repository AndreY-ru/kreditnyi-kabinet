"""ИИ-помощники: подбор по описанию своими словами и черновик сообщения клиенту для аналитика.

Модель в тестах подменяется: проверяем, что её ответы сверяются с текстом и правилами, а без модели
всё работает на правилах и шаблонах.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.config import settings
from app.services import client_message, intake, llm
from tests.helpers import SV, consent_all, draft, submit_and_process
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

# Фраза, где правила не уверены: цель без ключевых слов, две суммы и ни одной «нужно»
HARD = "Оборот 40 млн, 2 млн под ремонт и мебель для нового зала, года на полтора"


def test_clear_request_does_not_call_model(model):
    calls = model({"goal": "cash_gap", "amount_rub": 3_000_000, "term_months": 6})
    r = intake.parse(SEASON)
    assert calls == [] and r.source == "rules" and r.goal == "working"  # правила уверены — модель не нужна


def test_model_fills_only_gaps_of_rules(model):
    calls = model({"goal": "new_direction", "amount_rub": 2_000_000, "term_months": 18})
    r = intake.parse(HARD)
    assert (r.source, r.goal, r.amount, r.term) == ("llm", "new_direction", 2_000_000, 18)
    assert r.model_filled == ["цель", "сумму"]  # срок правила нашли сами
    assert calls[0]["format"]["required"] and calls[0]["think"] is False  # строгий формат, без рассуждений


def test_model_cannot_invent_amount(model):
    model({"goal": "new_direction", "amount_rub": 7_000_000, "term_months": 60})
    r = intake.parse(HARD)
    assert r.amount == 2_000_000 and r.term == 18  # 7 млн и 60 мес. в тексте нет — остаётся выбор правил
    assert r.notes and "нет в тексте" in r.notes[0] and "сумму" not in r.model_filled


def test_model_down_falls_back_to_rules(model):
    model(None)
    r = intake.parse(HARD)
    assert r.source == "rules" and r.amount == 2_000_000 and r.goal is None


def test_model_down_is_not_retried_on_every_request(model):
    calls = model(None)
    intake.parse(HARD)
    intake.parse(HARD)
    assert len(calls) == 1  # после сбоя полминуты не ждём таймаут на каждом запросе


def test_model_garbage_falls_back_to_rules(model):
    model("Конечно! Вот ваш ответ: сумма три миллиона")
    assert intake.parse(HARD).source == "rules"


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


# --- Черновик сообщения клиенту ---------------------------------------------------------------

OSV = "Оборотно-сальдовая ведомость по счёту 62"
NOTE = "нужна ОСВ 62, выручка не бьётся с БКИ"


def test_message_without_model_is_template():
    d = client_message.draft(NOTE, OSV)
    assert d.source == "template" and "счёту 62" in d.text


def test_message_from_model_is_accepted_when_clean(model):
    calls = model("Пожалуйста, приложите оборотно-сальдовую ведомость по счёту 62. "
                  "Она поможет нам подтвердить выручку компании и быстрее рассмотреть заявку.")
    d = client_message.draft(NOTE, OSV)
    assert d.source == "llm" and "ведомость" in d.text
    assert NOTE in calls[0]["messages"][-1]["content"]  # перед запросом — примеры


@pytest.mark.parametrize("answer, why", [
    ("Пожалуйста, приложите ОСВ 62: выручка не совпадает с данными БКИ, нам нужно это проверить.", "бки"),
    ("Пожалуйста, приложите ведомость по счёту 62 за 12 месяцев, и мы одобрим кредит.", "новые числа"),
    ("Please attach the trial balance for account 62 so that we can verify your revenue.", "не русский"),
])
def test_message_from_model_rejected_when_leaks_or_invents(model, answer, why):
    model(answer)
    d = client_message.draft(NOTE, OSV)
    assert d.source == "template" and why in d.rejected


def test_analyst_button_and_api(anna, maria, petr):
    app = draft(anna, SV, "WORKING", 5_000_000, 24)
    consent_all(anna, app["id"])
    submit_and_process(anna, app["id"])
    r = maria.post(f"/api/bank/applications/{app['id']}/client-message", json={"note": NOTE, "doc_kind": OSV})
    assert r.status_code == 200 and r.json()["source"] == "template"
    assert anna.post(f"/api/bank/applications/{app['id']}/client-message",
                     json={"note": NOTE, "doc_kind": OSV}).status_code == 403  # только банк

    c = web_login("maria@bank.local")
    page = c.post(f"/bank/applications/{app['id']}/client-message", data={"doc_kind": OSV, "info_note": NOTE},
                  headers={"HX-Request": "true"})
    assert page.status_code == 200 and 'id="icom-field"' in page.text and "счёту 62" in page.text
    assert "Сформулировать для клиента" in c.get(f"/bank/applications/{app['id']}").text


def test_forbidden_words_match_whole_word_start_only():
    """«ош-ибки» содержит «бки», но про бюро кредитных историй там ни слова — такой текст пропускаем."""
    ok = "Пожалуйста, приложите отчётность. Если цифры расходятся, это может указывать на ошибки в анкете."
    assert llm.safe_text(ok, source="", forbidden=client_message.INTERNAL) is None
    leak = "Пожалуйста, приложите отчётность: выручка не совпадает с данными БКИ."
    assert "бки" in llm.safe_text(leak, source="", forbidden=client_message.INTERNAL)


def test_safe_text_rules():
    src = "нужна ОСВ 62"
    assert llm.safe_text("Пожалуйста, приложите ведомость по счёту 62, она поможет нам.", source=src) is None
    assert llm.safe_text("Одобрим сразу после ведомости по счёту 62, обещаем.", source=src) is not None
    assert llm.numbers("1 000 000 и 2,5") == {"1000000", "2.5"}
