"""Подбор кредита по описанию своими словами.

«Нужно 3 млн на закупку товара к сезону, отдам за полгода, есть грузовик в залог» →
цель, сумма, срок, залог, поручитель. Дальше работает обычный подбор: этот модуль только заполняет форму,
клиент видит, как его поняли, и может поправить.

Два способа разбора:
- правила (работают всегда): находят суммы и сроки в тексте, цель — по ключевым словам;
- модель с открытыми весами (если включена): понимает контекст — какая из сумм нужна, где срок кредита,
  а где возраст бизнеса.
Числа модель может брать только из найденных в тексте: сумму или срок, которых клиент не писал, она
«придумать» не может. Не ответила или ответила странно — используем правила.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.services import llm
from app.services.catalog import GOALS, TERMS

# --- Числа в тексте ---------------------------------------------------------------------------

_WORD_NUM = {
    "один": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7,
    "восемь": 8, "девять": 9, "десять": 10, "пятнадцать": 15, "двадцать": 20, "тридцать": 30, "сорок": 40,
    "пятьдесят": 50, "сто": 100, "полтора": 1.5, "полторы": 1.5,
}
_NUM = r"(?<![а-я])(\d+(?:[.,]\d+)?|" + "|".join(_WORD_NUM) + r")"
_UNITS = [  # (шаблон единицы, множитель)
    (r"(?:млрд|миллиард\w*)", 1_000_000_000),
    (r"(?:млн|миллион\w*|лям\w*)", 1_000_000),
    (r"(?:тыс\w*|т\.\s?р\.?)", 1_000),
]
_MONEY_WITH_UNIT = re.compile(_NUM + r"\s*(" + "|".join(u for u, _ in _UNITS) + r")")
_MONEY_K = re.compile(r"(?<![\d.,])(\d+(?:[.,]\d+)?)к(?![а-я])")  # «500к» — только слитно, «5 к сезону» не сумма
_AMOUNT_TRIGGER = re.compile(r"(нужн\w*|надо|взять|занять|кредит\w*|сумм\w*|прошу|хочу|требует\w*|не хватает)\D{0,25}$")
_NOT_AMOUNT = re.compile(r"(выручк\w*|оборот\w*|прибыл\w*|доход\w*|долг\w*|остат\w*|должны|задолженн\w*)\D{0,15}$")
_PLAIN_MONEY = re.compile(r"(?<!\d)(?<!\d[.,])(\d{5,12})(?!\d|[.,]\d)")  # 50000, 3000000 (запятая после — не дробь)
_TERM_NUM = re.compile(_NUM + r"\s*(мес\w*|года?|лет)(?![а-я])")  # не «годовых» и не «годовой»
_MAX_TERM = 120
_TERM_WORDS = {"полгода": 6, "полугода": 6, "полтора года": 18, "квартал": 3, "год": 12, "года": 12}
_TERM_TRIGGER = re.compile(r"(на|за|через|срок\w*|верн\w*|отда\w*|погас\w*|в течени\w*|закро\w*)\s*[:—-]?\s*$")
_NOT_TERM = re.compile(r"((работа\w*|на рынке|существ\w*|бизнесу|компании|фирме|открыл\w*)\s+"
                       r"|(выручк\w*|оборот\w*|прибыл\w*|доход\w*)\s+(за|в)\s+)$")


def _num(token: str) -> float:
    return _WORD_NUM[token] if token in _WORD_NUM else float(token.replace(",", "."))


def _normalize(text: str) -> str:
    t = text.lower().replace("ё", "е").replace(" ", " ").replace(" ", " ")
    return re.sub(r"(?<=\d) (?=\d{3}(?!\d))", "", t)  # «3 000 000» → «3000000»


def money_candidates(text: str) -> list[tuple[int, int]]:
    """(позиция, сумма в рублях) — все суммы, которые можно прочитать в тексте."""
    t = _normalize(text)
    out: list[tuple[int, int]] = []
    taken: list[range] = []
    for m in _MONEY_WITH_UNIT.finditer(t):
        mult = next(k for u, k in _UNITS if re.fullmatch(u, m.group(2)))
        out.append((m.start(), int(round(_num(m.group(1)) * mult))))
        taken.append(range(m.start(), m.end()))
    for m in _MONEY_K.finditer(t):
        out.append((m.start(), int(round(_num(m.group(1)) * 1_000))))
        taken.append(range(m.start(), m.end()))
    for m in re.finditer(r"(?<![а-я])(?:пол\s?миллиона|полмиллиона)", t):
        out.append((m.start(), 500_000))
    for m in re.finditer(r"(?<![а-я])(миллион|миллиона)(?![а-я])", t):
        if not any(m.start() in r for r in taken):
            out.append((m.start(), 1_000_000))
    for m in _PLAIN_MONEY.finditer(t):
        if not any(m.start() in r for r in taken):
            out.append((m.start(), int(m.group(1))))
    return sorted(out)


def term_candidates(text: str) -> list[tuple[int, int, bool]]:
    """(позиция, срок в месяцах, похоже ли на срок кредита по словам перед ним)."""
    t = _normalize(text)
    out: list[tuple[int, int, bool]] = []
    taken: list[range] = []
    for m in _TERM_NUM.finditer(t):
        months = _num(m.group(1)) * (1 if m.group(2).startswith("мес") else 12)
        if not 0 < months <= _MAX_TERM:  # «2025 года» — дата, а не срок
            continue
        out.append((m.start(), int(round(months)), _looks_like_term(t, m.start())))
        taken.append(range(m.start(), m.end()))
    for words, months in _TERM_WORDS.items():
        for m in re.finditer(r"(?<![а-я])" + words + r"(?![а-я])", t):
            if not any(m.start() in r for r in taken):
                out.append((m.start(), months, _looks_like_term(t, m.start())))
                taken.append(range(m.start(), m.end()))
    return sorted(out)


def _looks_like_term(t: str, pos: int) -> bool:
    before = t[max(0, pos - 30):pos]
    return bool(_TERM_TRIGGER.search(before)) and not _NOT_TERM.search(before)


# --- Цель, залог, поручитель -----------------------------------------------------------------

_GOAL_RULES = [  # порядок важен: от более конкретного к общему
    ("refinance", r"рефинанс|перекредит|(погасить|закрыть) (\w+ )?(кредит|долг)|кредит в другом банке"),
    ("cash_gap", r"кассов\w* разрыв|зарплат|налоги заплатить|заплатить налоги|пока (не )?(заплат|оплат|при[дш]|перевед)"
                 r"|задерживают оплат|отсрочк\w* платеж|дебитор"),
    ("equipment", r"(куп\w*|приобр\w*|обнов\w*|замен\w*|взять в лизинг|на)\s+(\w+\s+){0,3}"
                  r"(оборудован|станк|станок|техник|машин|автомоб|грузовик|трактор|фургон|печ|лини|экскаватор"
                  r"|компьютер|кофемашин|холодильн)|оборудовани"),
    ("new_direction", r"нов\w* направлени|открыть (\w+ )?(точк|магазин|филиал|кофейн|салон|цех|склад|производств)"
                      r"|расшир\w*|запуст\w*|франшиз"),
    ("working", r"закуп|товар|сыр[ье]|материал|сезон|оборот|запас|пополн"),
]
_NO_COLLATERAL = re.compile(r"без залога|залога нет|нечего (заложить|в залог)|нет залога")
_COLLATERAL = re.compile(r"залог|заложить|под обеспечени")
_NO_GUARANTOR = re.compile(r"без поручит|поручител\w* нет|нет поручител")
_GUARANTOR = re.compile(r"поручит|поручусь|поручиться")


@dataclass
class Intake:
    goal: str | None = None
    amount: int | None = None
    term: int | None = None
    collateral: bool | None = None
    guarantor: bool | None = None
    source: str = "rules"  # rules — правила, llm — модель (числа сверены с текстом)
    notes: list[str] = field(default_factory=list)

    @property
    def missing(self) -> list[str]:
        return [name for name, v in (("цель", self.goal), ("сумму", self.amount), ("срок", self.term)) if v is None]

    def summary(self) -> str:
        parts = []
        if self.goal:
            parts.append(GOALS[self.goal].lower())
        if self.amount:
            parts.append(f"{self.amount:,} ₽".replace(",", " "))
        if self.term:
            parts.append(f"на {self.term} мес.")
        if self.collateral is not None:
            parts.append("есть залог" if self.collateral else "без залога")
        if self.guarantor is not None:
            parts.append("с поручительством собственника" if self.guarantor else "без поручителя")
        return ", ".join(parts)


def parse_rules(text: str) -> Intake:
    t = _normalize(text)
    res = Intake(source="rules")
    res.goal = next((g for g, rx in _GOAL_RULES if re.search(rx, t)), None)
    res.amount = _pick_amount(t, money_candidates(text))
    terms = [x for x in term_candidates(text) if x[2]]
    if terms:
        res.term = terms[0][1]
    res.collateral = False if _NO_COLLATERAL.search(t) else (True if _COLLATERAL.search(t) else None)
    res.guarantor = False if _NO_GUARANTOR.search(t) else (True if _GUARANTOR.search(t) else None)
    return res


def _pick_amount(t: str, money: list[tuple[int, int]]) -> int | None:
    """Сумма после «нужно / взять / кредит», а не выручка или долги; иначе первая подходящая."""
    if not money:
        return None
    asked = [v for pos, v in money if _AMOUNT_TRIGGER.search(t[max(0, pos - 40):pos])]
    if asked:
        return asked[0]
    other = [v for pos, v in money if not _NOT_AMOUNT.search(t[max(0, pos - 30):pos])]
    return (other or [money[0][1]])[0]


# --- Модель --------------------------------------------------------------------------------

_SCHEMA = {
    "type": "object",
    "properties": {
        "goal": {"type": "string", "enum": [*GOALS, "unknown"]},
        "amount_rub": {"type": "integer"},
        "term_months": {"type": "integer"},
        "collateral": {"type": "string", "enum": ["yes", "no", "unknown"]},
        "guarantor": {"type": "string", "enum": ["yes", "no", "unknown"]},
    },
    "required": ["goal", "amount_rub", "term_months", "collateral", "guarantor"],
}

_SYSTEM = (
    "Ты разбираешь запрос предпринимателя на кредит для бизнеса и заполняешь форму. Отвечай только JSON.\n"
    "goal: working — пополнить оборотные средства (закупка товара, сырья, сезон); equipment — купить "
    "оборудование, технику, транспорт; cash_gap — закрыть кассовый разрыв (зарплата, налоги, задержка оплаты "
    "от покупателей); new_direction — новое направление, новая точка, расширение; refinance — погасить кредит "
    "в другом банке; unknown — непонятно.\n"
    "amount_rub — сумма кредита в рублях (3 млн = 3000000), 0 если не указана. Не путай с выручкой и долгами.\n"
    "term_months — на сколько месяцев нужен кредит (полгода = 6, 2 года = 24), 0 если не указан. "
    "Не путай со сроком работы бизнеса.\n"
    "collateral — есть ли что дать банку в залог; guarantor — готов ли собственник стать поручителем."
)


def parse_llm(text: str) -> dict | None:
    return llm.chat_json(_SYSTEM, text[:1000], _SCHEMA)


def parse(text: str) -> Intake:
    """Правила + модель. Числа модели принимаем, только если они есть в тексте."""
    rules = parse_rules(text)
    data = parse_llm(text) if llm.enabled() and text.strip() else None
    if data is None:
        return rules
    res = Intake(source="llm")
    goal = data.get("goal")
    res.goal = goal if goal in GOALS else rules.goal

    money = {v for _, v in money_candidates(text)}
    amount = data.get("amount_rub")
    if isinstance(amount, int) and amount > 0 and amount in money:
        res.amount = amount
    else:
        res.amount = rules.amount
        if isinstance(amount, int) and amount > 0:
            res.notes.append(f"модель предложила сумму {amount}, которой нет в тексте, — взяли из текста")

    terms = {v for _, v, _ in term_candidates(text)}
    term = data.get("term_months")
    res.term = term if isinstance(term, int) and term > 0 and term in terms else rules.term

    res.collateral = {"yes": True, "no": False}.get(data.get("collateral"), rules.collateral)
    res.guarantor = {"yes": True, "no": False}.get(data.get("guarantor"), rules.guarantor)
    return res


def nearest_term(months: int) -> int:
    """Срок для переключателя в подборе (6, 12, 18, 24, 36, 60 мес.)."""
    return min(TERMS, key=lambda t: (abs(t - months), -t))
