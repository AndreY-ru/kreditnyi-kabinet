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
from dataclasses import dataclass, field, replace

from app.services import llm
from app.services.catalog import GOALS, TERMS
from app.services.scoring import DEFAULT_MODEL

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
_AMOUNT_TRIGGER = re.compile(r"(нуж\w*|надо|взять|занять|кредит\w*|сумм\w*|прошу|хочу|требует\w*|не хватает)\D{0,25}$")
_NOT_AMOUNT = re.compile(r"(выручк\w*|оборот\w*|прибыл\w*|доход\w*|долг\w*|остат\w*|должны|задолженн\w*)\D{0,15}$")
_PLAIN_MONEY = re.compile(r"(?<!\d)(?<!\d[.,])(\d{5,12})(?!\d|[.,]\d)")  # 50000, 3000000 (запятая после — не дробь)
_TERM_NUM = re.compile(_NUM + r"\s*(мес\w*|года?|лет)(?![а-я])")  # не «годовых» и не «годовой»
_MAX_TERM = 120
_TERM_WORDS = {"полгода": 6, "полугода": 6, "полтора года": 18, "квартал": 3, "год": 12, "года": 12,
               "пару месяцев": 2, "пару лет": 24, "пару годиков": 24}
_TERM_REVERSED = re.compile(r"(?<![а-я])(месяц\w*|мес|года?|лет)\s+на\s+" + _NUM[len("(?<![а-я])"):])  # «месяца на четыре»
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
    for m in _TERM_REVERSED.finditer(t):
        months = _num(m.group(2)) * (1 if m.group(1).startswith("мес") else 12)
        if 0 < months <= _MAX_TERM and not any(m.start() in r for r in taken):
            out.append((m.start(), int(round(months)), True))  # «лет на пять» — так говорят о сроке кредита
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
    ("cash_gap", r"разрыв|зарплат|налоги заплатить|заплатить налоги|пока (не )?(заплат|оплат|при[дш]|перевед)"
                 r"|задерживают оплат|отсрочк\w* платеж|дебитор"),
    ("equipment", r"(куп\w*|приобр\w*|обнов\w*|замен\w*|взять в лизинг|на)\s+(\w+\s+){0,3}"
                  r"(оборудован|станк|станок|техник|машин|автомоб|грузовик|трактор|фургон|печ|лини|экскаватор"
                  r"|компьютер|кофемашин|холодильн)|оборудовани"),
    ("new_direction", r"нов\w* направлени|открыть (\w+ )?(точк|магазин|филиал|кофейн|салон|цех|склад|производств)"
                      r"|расшир\w*|запуст\w*|запуска\w*|франшиз"),
    ("working", r"закуп|товар|сыр[ье]|материал|сезон|оборотн|оборотк|запас|пополн"),  # не «оборот 120 млн»
]
_NO_COLLATERAL = re.compile(r"без залога|залога нет|нечего (заложить|в залог)|нет залога")
_COLLATERAL = re.compile(r"залог|заложить|под обеспечени")
# Что в залоге: берём вид имущества, названный ближе всего к слову «залог» («закупка товара» — это цель)
_COLLATERAL_KINDS = [
    ("REAL_ESTATE", r"недвижим|помещени|склад|здани|квартир|офис|земл|участ|цех"),
    ("VEHICLE", r"грузовик|машин|автомоб|транспорт|спецтехник|трактор|фургон|экскаватор|газел"),
    ("EQUIPMENT", r"оборудовани|станк|станок|линию|линия"),
    ("GOODS", r"товар|запас|сырь|продукци"),
    ("DEPOSIT", r"депозит|вклад"),
]
_KIND_NAMES = {k: v["name"] for k, v in DEFAULT_MODEL["credit"]["collateral"].items()}
_NO_GUARANTOR = re.compile(r"без поручит|поручител\w* нет|нет поручител")
_GUARANTOR = re.compile(r"поручит|поручусь|поручиться")


@dataclass
class Intake:
    goal: str | None = None
    amount: int | None = None
    term: int | None = None
    collateral: bool | None = None
    collateral_kind: str | None = None
    guarantor: bool | None = None
    source: str = "rules"  # rules — только правила; llm — модель заполнила пробелы (model_filled)
    notes: list[str] = field(default_factory=list)
    model_filled: list[str] = field(default_factory=list)

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
            kind = _KIND_NAMES.get(self.collateral_kind or "")
            parts.append(("залог: " + kind.lower() if kind else "есть залог") if self.collateral else "без залога")
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
    if res.collateral:
        res.collateral_kind = collateral_kind(t)
    res.guarantor = False if _NO_GUARANTOR.search(t) else (True if _GUARANTOR.search(t) else None)
    return res


def collateral_kind(t: str) -> str | None:
    anchors = [m.start() for m in _COLLATERAL.finditer(t)]
    best = None
    for kind, rx in _COLLATERAL_KINDS:
        for m in re.finditer(rx, t):
            dist = min(abs(m.start() - a) for a in anchors)
            if dist <= 40 and (best is None or dist < best[0]):
                best = (dist, kind)
    return best[1] if best else None


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
    },
    "required": ["goal", "amount_rub", "term_months"],
}

_SYSTEM = (
    "Ты разбираешь запрос предпринимателя на кредит для бизнеса. Отвечай только JSON.\n"
    "goal: working — оборотные средства (закупка товара, сырья, сезон); equipment — оборудование, техника, "
    "транспорт, мебель; cash_gap — кассовый разрыв (зарплата, налоги, задержка оплаты); new_direction — новое "
    "направление, новая точка, новый зал, расширение; refinance — погасить кредит в другом банке; unknown.\n"
    "amount_rub — сумма кредита в рублях (3 млн = 3000000), 0 если нет. Не выручка и не долги.\n"
    "term_months — на сколько месяцев кредит (полгода = 6, 2 года = 24), 0 если нет. Не возраст бизнеса."
)


def _gaps(text: str, rules: Intake) -> set[str]:
    """Где правила не уверены — только там спрашиваем модель."""
    t = _normalize(text)
    gaps = set()
    if rules.goal is None:
        gaps.add("goal")
    money = money_candidates(text)
    if len({v for _, v in money}) > 1 and not any(_AMOUNT_TRIGGER.search(t[max(0, p - 40):p]) for p, _ in money):
        gaps.add("amount")  # несколько сумм и непонятно, какая просится
    if rules.term is None and term_candidates(text):
        gaps.add("term")
    return gaps


def parse(text: str) -> Intake:
    """Сначала правила (мгновенно). Модель — только для пробелов, и её числа должны быть в тексте."""
    rules = parse_rules(text)
    gaps = _gaps(text, rules)
    if not gaps or not llm.enabled():
        return rules
    data = llm.chat_json(_SYSTEM, text[:1000], _SCHEMA, max_tokens=60)
    if data is None:
        return rules
    res = replace(rules, notes=list(rules.notes))
    if "goal" in gaps and data.get("goal") in GOALS:
        res.goal = data["goal"]
        res.model_filled.append("цель")
    amount = data.get("amount_rub")
    if "amount" in gaps and isinstance(amount, int) and amount > 0:
        if amount in {v for _, v in money_candidates(text)}:
            res.amount = amount
            res.model_filled.append("сумму")
        else:
            res.notes.append(f"модель предложила сумму {amount}, которой нет в тексте, — не взяли")
    term = data.get("term_months")
    if "term" in gaps and isinstance(term, int) and term in {v for _, v, _ in term_candidates(text)}:
        res.term = term
        res.model_filled.append("срок")
    if res.model_filled:
        res.source = "llm"
    return res


def nearest_term(months: int) -> int:
    """Срок для переключателя в подборе (6, 12, 18, 24, 36, 60 мес.)."""
    return min(TERMS, key=lambda t: (abs(t - months), -t))
