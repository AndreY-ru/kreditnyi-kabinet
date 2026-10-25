"""Калибровка скоринговой карты: логистическая регрессия на портфеле с известными исходами → баллы и пороги.

    python tools/calibrate.py                     # синтетический портфель (истории дефолтов у нас нет)
    python tools/calibrate.py --data history.csv  # история банка: поля ScoringInputs + default (0/1)

1. Портфель. Без истории банка — синтетический (generate): у каждой компании выручка, маржа, долги, срок
   работы, динамика, история платежей и ненаблюдаемое «качество управления». Дефолт за 12 месяцев не
   задаётся баллами, а возникает сам: денежного потока не хватает на платежи (выручка колеблется, платежи
   фиксированы) или бизнес закрывается по нефинансовым причинам (чаще у молодых и при слабом управлении).
   Параметры генератора привязаны к открытым данным — таблица в docs/calibration.md.
2. Корзины факторов — той же функцией, что в скоринге (scoring.levels).
3. Логистическая регрессия (Ньютон — Рафсон с небольшой L2-регуляризацией), без внешних библиотек.
4. Баллы в шкале шансов: 60 баллов ↔ PD 5%, каждые 10 баллов — шансы дефолта вдвое ниже.
5. Пороги — из риск-аппетита, а не подбором: отказ, если ожидаемые потери PD × LGD без залога больше
   максимальной надбавки к ставке (5 п. п.); автоодобрение — при PD не выше 3%.
6. Проверка на отложенной трети портфеля тем же кодом, что в продукте (scoring.score): Gini, прогноз PD
   против факта по диапазонам баллов, доли зон, дефолтность при покрытии платежей ниже и выше нормы.

Результат — app/services/scorecard.json (его читает scoring.py) и отчёт docs/calibration.md.
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.scoring import DEFAULT_MODEL, ScoringInputs, levels, pd_of, score, score_of  # noqa: E402

OUT = ROOT / "app" / "services" / "scorecard.json"
REPORT = ROOT / "docs" / "calibration.md"

VERSION = "v1.0"
# Открытые нормативы и данные, на которые опираются допущения (нумерация — как в отчёте)
SOURCES = [
    "Положение Банка России от 28.06.2017 № 590-П «О порядке формирования кредитными организациями резервов на "
    "возможные потери по ссудам…», п. 3.7 — качество обслуживания долга: "
    "[Гарант](https://base.garant.ru/71721612/5ac206a89ea76855804609cd950fcaf7/)",
    "Basel Committee on Banking Supervision, Basel Framework, CRE32 «IRB approach: risk components» — LGD 40% для "
    "необеспеченных требований к нефинансовым компаниям (CRE32.6), LGD обеспеченной части и дисконт 40% (CRE32.11): "
    "[BIS](https://www.bis.org/committees/bcbs/basel-framework/81366/chapter.pdf)",
    "EBA, Final draft RTS on the materiality threshold for credit obligations past due (EBA/RTS/2016/06) — порог "
    "существенности просрочки 500 евро и 1%, дефолт после 90 дней: "
    "[EBA](https://www.eba.europa.eu/documents/10180/1597002/Final+draft+RTS+on+the+materiality+threshold+for+credit+obligations+%28EBA-RTS-2016-06%29.pdf/fe1db887-c6dc-4777-89c1-4f243584cafd)",
    "Эксперт РА, «Рынок кредитования малого и среднего бизнеса по итогам 2025 года и прогноз на 2026-й: "
    "нереальный МСБ», 30.04.2026 — доля просрочки в портфеле МСБ снизилась с 4,4 до 4%: "
    "[raexpert.ru](https://raexpert.ru/researches/banks/msb_2025)",
    "Исследование Точка Банка ко Дню российского предпринимательства (пересказ Mail.ru Финансов, 26.05.2026) — из "
    "компаний и ИП, зарегистрированных в 2022 году, к концу 2025-го работает половина: "
    "[Mail.ru Финансы](https://finance.mail.ru/article/v-rossii-iz-otkryityih-tri-goda-nazad-kompanij-zakryilas-kazhdaya-vtoraya-69210367/)",
]
SCALE = {"score": 60, "pd": 0.05, "pdo": 10}
APPETITE = {"auto_max_pd": 0.03, "max_risk_premium": 0.05}
LEVELS = {
    "business_age": [0, 1, 2, 3, 4],
    "payment_discipline": ["clean", "minor_old", "minor_recent", "serious", "default"],
    "guarantor": ["no", "yes"],
    "revenue_trend": ["GROWTH", "FLAT", "DECLINE"],
    "request_to_revenue": [0, 1, 2, 3],
    "debt_load": [0, 1, 2, 3, 4, "no_profit"],
    "tax_arrears": ["none", "small", "large"],
}
# У опорного уровня 0 баллов: остальные — плюс или минус относительно «обычного» случая
REFERENCE = {"business_age": 1, "payment_discipline": "minor_old", "guarantor": "no", "revenue_trend": "FLAT",
             "request_to_revenue": 0, "debt_load": 0, "tax_arrears": "none"}
# Чем «правее» уровень, тем хуже (для проверки монотонности); у срока бизнеса — наоборот
ORDERED = {"business_age": +1, "payment_discipline": -1, "request_to_revenue": -1, "debt_load": -1, "tax_arrears": -1}
# Уровни упорядоченных факторов от меньшего риска к большему: риск не может убывать вдоль этого порядка
RISK_ORDER = {"business_age": [4, 3, 2, 1, 0],
              "payment_discipline": ["clean", "minor_old", "minor_recent", "serious", "default"],
              "request_to_revenue": [0, 1, 2, 3], "debt_load": [0, 1, 2, 3, 4, "no_profit"],
              "tax_arrears": ["none", "small", "large"]}


# --- 1. Синтетический портфель -----------------------------------------------------------------------

def _annuity(amount: float, rate_pct: float, months: int) -> float:
    r = rate_pct / 100 / 12
    return amount * r / (1 - (1 + r) ** -months) if months > 0 else amount


def _poisson(rng: random.Random, lam: float) -> int:
    k, p, limit = 0, 1.0, math.exp(-lam)
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def generate(n: int, seed: int = 2026) -> list[tuple[ScoringInputs, int, float]]:
    """(входы скоринга, дефолт за 12 мес. 0/1, покрытие платежей) для n синтетических заявок."""
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        q = rng.gauss(0, 1)  # качество управления: не видно банку, проявляется в истории платежей и налогах
        u = rng.random()
        age = (rng.randint(3, 11) if u < 0.15 else rng.randint(12, 35) if u < 0.35
               else rng.randint(36, 59) if u < 0.55 else rng.randint(60, 240))
        fresh = math.exp(-age / 18)  # «молодость»: около 1 у новой компании, почти 0 к пяти годам
        revenue = min(max(math.exp(rng.gauss(math.log(40e6) - 1.6 * fresh, 0.9)), 1.5e6), 1.5e9)
        margin = max(rng.gauss(0.10 - 0.05 * fresh, 0.05 + 0.04 * fresh) + 0.015 * q, -0.15)
        ebitda = revenue * margin  # так в анкете; отчётность МСБ неточна — настоящая маржа другая
        true_ebitda = revenue * (margin + rng.gauss(0, 0.04))
        amort = max(ebitda, 0) * 0.15
        p_growth = min(max(0.35 + 0.08 * q, 0.05), 0.8)
        p_decline = min(max(0.20 - 0.06 * q, 0.03), 0.6)
        v = rng.random()
        trend = "GROWTH" if v < p_growth else ("DECLINE" if v > 1 - p_decline else "FLAT")
        growth = {"GROWTH": 0.15, "FLAT": 0.0, "DECLINE": -0.25}[trend] + rng.gauss(0, 0.10)

        debt = max(ebitda, 0.03 * revenue) * math.exp(rng.gauss(math.log(0.8), 0.7)) if rng.random() < 0.6 else 0.0
        existing_monthly = _annuity(debt, 16, rng.choice([36, 48, 60])) if debt else 0.0
        ratio = min(max(math.exp(rng.gauss(math.log(0.05), 0.8)), 0.005), 0.8)
        amount = max(round(revenue * ratio, -4), 100_000)
        term = rng.choice([6, 12, 12, 18, 24, 24, 36, 48, 60])
        guarantor = rng.random() < 0.7

        # История платежей за 24 мес.: эпизоды чаще у слабого управления и при низкой марже. Управление
        # со временем меняется, поэтому свежие эпизоды говорят о нём больше: от свежести зависит частота
        stress = 1.6 if margin < 0.02 else 1.0
        overdues = []
        for recent, lam in ((True, 0.10 * math.exp(-1.5 * q) * stress), (False, 0.10 * math.exp(-0.8 * q) * stress)):
            for _ in range(_poisson(rng, lam)):
                w = [0.40 * math.exp(0.4 * q), 0.35, 0.15 * math.exp(-1.0 * q), 0.03 * math.exp(-1.5 * q)]
                kind = rng.choices(range(4), weights=w)[0]
                days = [rng.randint(1, 5), rng.randint(6, 30), rng.randint(31, 90), rng.randint(91, 180)][kind]
                small = kind == 0 or rng.random() < 0.15  # мелкая сумма — техническая просрочка
                amt = rng.randint(1_000, 45_000) if small else int(max(55_000, revenue / 120 * rng.uniform(0.3, 1.5)))
                overdues.append({"days": days, "amount": amt,
                                 "months_ago": rng.randint(0, 11) if recent else rng.randint(12, 23)})
        p_tax = 0.05 * math.exp(-0.8 * q) * (2.0 if margin < 0 else 1.0)
        tax = int(math.exp(rng.gauss(math.log(70_000) - 0.6 * q, 1.0))) if rng.random() < p_tax else 0

        inp = ScoringInputs(
            amount=int(amount), term_months=term, business_months=age, company_status="ACTIVE",
            min_business_months=0, revenue=int(revenue), ebit=int(ebitda - amort), amortization=int(amort),
            revenue_trend=trend, existing_debt=int(debt), tax_arrears=tax, has_collateral=False,
            has_guarantor=guarantor, bki_loans=[{"balance": int(debt), "overdues_24m": overdues}],
            existing_monthly=int(existing_monthly) or None,
        )

        # Следующие 12 месяцев: денежный поток против платежей. Короткие кредиты в жизни частично
        # рефинансируют и гасят из оборота, поэтому тело нового долга растягиваем минимум на 2 года
        pay = existing_monthly + _annuity(amount, 20, max(term, 24))
        sigma = 0.25 + 0.20 * fresh  # помесячные колебания прибыли
        year = rng.gauss(0, 0.20 + 0.10 * fresh)  # удачный или неудачный год — на весь период
        # подушка: оборотные деньги + 2–5 месяцев прибыли; поручитель-собственник закроет один платёж
        cash = 0.03 * revenue + max(true_ebitda / 12, 0) * (2 + 1.5 * max(q + 1, 0)) + (pay if guarantor else 0)
        # потеря ключевого клиента или поставщика: маржа падает на 6 п. п. до конца года
        p_shock = 0.05 + 0.08 * fresh + (0.08 if trend == "DECLINE" else 0)  # падение выручки — часто беда рынка
        shock = rng.randint(1, 12) if rng.random() < p_shock else 13
        default, missed = 0, 0
        for t in range(1, 13):
            cash += true_ebitda / 12 * (1 + growth) ** (t / 12) * math.exp(year + sigma * rng.gauss(0, 1) - sigma ** 2 / 2)
            if t >= shock:
                cash -= 0.06 * revenue / 12
            if cash >= pay:
                cash -= pay
                missed = 0
            else:
                missed += 1
                if missed >= 3:  # три платежа подряд не внесены — просрочка больше 90 дней, дефолт
                    default = 1
                    break
        # Нефинансовые причины: закрытие, споры, уход собственника
        hazard = 0.004 * math.exp(-1.9 * q) * (1 + 2.0 * fresh) * (0.75 if guarantor else 1)
        if not default and rng.random() < hazard:
            default = 1
        dscr = max(ebitda, 0) / (12 * pay)
        out.append((inp, default, dscr))
    return out


def read_history(path: Path) -> list[tuple[ScoringInputs, int, float]]:
    """История банка: колонки ScoringInputs, overdues — JSON-список просрочек, default — 0/1."""
    rows = []
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            num = {k: int(float(r[k])) if r.get(k) not in (None, "") else None for k in (
                "amount", "term_months", "business_months", "revenue", "ebit", "amortization", "existing_debt",
                "tax_arrears", "existing_monthly")}
            inp = ScoringInputs(
                amount=num["amount"], term_months=num["term_months"], business_months=num["business_months"],
                company_status="ACTIVE", min_business_months=0, revenue=num["revenue"], ebit=num["ebit"],
                amortization=num["amortization"], revenue_trend=r.get("revenue_trend") or None,
                existing_debt=num["existing_debt"] or 0, tax_arrears=num["tax_arrears"] or 0, has_collateral=False,
                has_guarantor=r.get("has_guarantor") in ("1", "true", "True"),
                bki_loans=[{"balance": num["existing_debt"] or 0, "overdues_24m": json.loads(r.get("overdues") or "[]")}],
                existing_monthly=num["existing_monthly"])
            rows.append((inp, int(r["default"]), float("nan")))
    return rows


# --- 2–3. Логистическая регрессия --------------------------------------------------------------------

def features() -> list[tuple[str, object]]:
    return [(code, lv) for code, lvs in LEVELS.items() for lv in lvs if lv != REFERENCE[code]]


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        for r in range(n):
            if r != c and m[r][c]:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    return [m[i][n] / m[i][i] for i in range(n)]


def fit(data: list[tuple[ScoringInputs, int, float]], ridge: float = 1.0) -> tuple[dict, dict]:
    """Коэффициенты логистической регрессии P(дефолт) по корзинам факторов (ключ ("_", "_") — свободный член)
    и число заявок в каждой корзине."""
    feats = features()
    index = {f: i + 1 for i, f in enumerate(feats)}
    groups: dict[tuple[int, ...], list[int]] = {}  # одинаковые сочетания корзин считаем один раз
    counts: dict[tuple[str, object], int] = {}
    for inp, y, _ in data:
        lv = levels(inp, DEFAULT_MODEL)
        for c, v in lv.items():
            counts[(c, v)] = counts.get((c, v), 0) + 1
        key = (0,) + tuple(index[(c, v)] for c, v in lv.items() if (c, v) in index)
        g = groups.setdefault(key, [0, 0])
        g[0] += 1
        g[1] += y
    n = len(feats) + 1
    beta = [0.0] * n
    beta[0] = math.log(sum(g[1] for g in groups.values()) / sum(g[0] for g in groups.values()))
    for _ in range(50):
        grad = [0.0] * n
        hess = [[0.0] * n for _ in range(n)]
        for key, (cnt, dft) in groups.items():
            p = 1 / (1 + math.exp(-min(max(sum(beta[i] for i in key), -35), 35)))
            r, w = dft - cnt * p, cnt * p * (1 - p)
            for i in key:
                grad[i] += r
                row = hess[i]
                for j in key:
                    row[j] += w
        for i in range(1, n):
            grad[i] -= ridge * beta[i]
            hess[i][i] += ridge
        step = _solve(hess, grad)
        big = max(abs(s) for s in step)
        if big > 2:  # первые шаги Ньютона могут перелетать — ограничиваем
            step = [s * 2 / big for s in step]
        beta = [b + s for b, s in zip(beta, step)]
        if max(abs(s) for s in step) < 1e-9:
            break
    return {("_", "_"): beta[0], **{f: beta[index[f]] for f in feats}}, counts


def _pava(values: list[float], weights: list[float]) -> list[float]:
    """Изотоническая поправка: ближайшая неубывающая последовательность (соседи-нарушители объединяются)."""
    blocks = [[v, w, 1] for v, w in zip(values, weights)]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][0] > blocks[i + 1][0]:
            a, b = blocks[i], blocks[i + 1]
            blocks[i] = [(a[0] * a[1] + b[0] * b[1]) / (a[1] + b[1]), a[1] + b[1], a[2] + b[2]]
            del blocks[i + 1]
            i = max(i - 1, 0)
        else:
            i += 1
    return [v for v, _, k in blocks for _ in range(k)]


def monotone(beta: dict, counts: dict) -> dict:
    """Риск не убывает вдоль порядка корзин: где регрессия из-за шума нарушила порядок — объединяем корзины."""
    beta = dict(beta)
    for code, order in RISK_ORDER.items():
        vals = [0.0 if lv == REFERENCE[code] else beta[(code, lv)] for lv in order]
        fixed = _pava(vals, [max(counts.get((code, lv), 0), 1) for lv in order])
        ref = fixed[order.index(REFERENCE[code])]  # опорный уровень снова нулевой, сдвиг — в свободный член
        for lv, v in zip(order, fixed):
            if lv != REFERENCE[code]:
                beta[(code, lv)] = v - ref
        beta[("_", "_")] += ref
    return beta


# --- 4–5. Баллы и пороги ------------------------------------------------------------------------------

def to_scorecard(beta: dict[tuple[str, object], float]) -> dict:
    b = SCALE["pdo"] / math.log(2)
    a = SCALE["score"] - b * math.log((1 - SCALE["pd"]) / SCALE["pd"])

    def pts(code: str, lv: object) -> int:
        return 0 if lv == REFERENCE[code] else round(-b * beta[(code, lv)])

    p = {code: {lv: pts(code, lv) for lv in lvs} for code, lvs in LEVELS.items()}
    lgd = DEFAULT_MODEL["credit"]["lgd_unsecured"]
    cfg_scale = {"scale": SCALE}
    return {
        "version": VERSION,
        "scale": SCALE,
        "base": round(a - b * beta[("_", "_")]),
        "zones": {"decline_below": math.ceil(score_of(APPETITE["max_risk_premium"] / lgd, cfg_scale)),
                  "auto_from": math.ceil(score_of(APPETITE["auto_max_pd"], cfg_scale))},
        "points": {
            "business_age": [p["business_age"][i] for i in range(5)],
            "payment_discipline": p["payment_discipline"],
            "guarantor": p["guarantor"]["yes"],
            "revenue_trend": p["revenue_trend"],
            "request_to_revenue": [p["request_to_revenue"][i] for i in range(4)],
            "debt_load": [p["debt_load"][i] for i in range(5)],
            "debt_load_no_profit": p["debt_load"]["no_profit"],
            "tax_arrears": {"small": p["tax_arrears"]["small"], "large": p["tax_arrears"]["large"]},
        },
    }


def apply(card: dict) -> dict:
    """Конфигурация модели с новыми баллами — та же сборка, что в scoring.py."""
    cfg = copy.deepcopy(DEFAULT_MODEL)
    pt = card["points"]
    cfg.update(version=card["version"], scale=card["scale"], base=card["base"], zones=card["zones"],
               guarantor=pt["guarantor"], revenue_trend=pt["revenue_trend"], debt_load_no_profit=pt["debt_load_no_profit"])
    for code in ("business_age", "request_to_revenue", "debt_load"):
        cfg[code] = [[u, v] for (u, _), v in zip(cfg[code], pt[code])]
    cfg["payment_discipline"] = {**cfg["payment_discipline"], "points": pt["payment_discipline"]}
    cfg["tax_arrears"] = {**cfg["tax_arrears"], **pt["tax_arrears"]}
    return cfg


def monotonic_problems(card: dict) -> list[str]:
    p = card["points"]
    seqs = {"business_age": p["business_age"], "request_to_revenue": p["request_to_revenue"],
            "debt_load": p["debt_load"] + [p["debt_load_no_profit"]],
            "payment_discipline": [p["payment_discipline"][k] for k in LEVELS["payment_discipline"]],
            "tax_arrears": [0, p["tax_arrears"]["small"], p["tax_arrears"]["large"]]}
    bad = []
    for code, s in seqs.items():
        d = ORDERED[code]
        if any((b - a) * d < 0 for a, b in zip(s, s[1:])):
            bad.append(f"{code}: {s}")
    return bad


# --- 6. Проверка ----------------------------------------------------------------------------------------

def gini(scores: list[int], ys: list[int]) -> float:
    """Gini = 2·AUC − 1; AUC — вероятность, что у случайного «хорошего» оценка выше, чем у «плохого»."""
    pairs = sorted(zip(scores, ys))
    rank, i, pos_ranks = 1, 0, 0.0
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        avg = (rank + rank + (j - i) - 1) / 2
        pos_ranks += avg * sum(1 for k in range(i, j) if pairs[k][1] == 0)
        rank += j - i
        i = j
    n_good = sum(1 for y in ys if y == 0)
    n_bad = len(ys) - n_good
    auc = (pos_ranks - n_good * (n_good + 1) / 2) / (n_good * n_bad)
    return 2 * auc - 1


def evaluate(card: dict, data: list[tuple[ScoringInputs, int, float]]) -> dict:
    cfg = apply(card)
    res = [(score(inp, cfg), y, dscr) for inp, y, dscr in data]
    scores = [r[0].score for r in res]
    ys = [r[1] for r in res]
    z1, z3 = card["zones"]["decline_below"], card["zones"]["auto_from"]
    bands = [(0, z1), (z1, 56), (56, z3), (z3, 80), (80, 101)]
    table = []
    for lo, hi in bands:
        rows = [r for r in res if lo <= r[0].score < hi]
        if rows:
            table.append({"band": f"{lo}–{hi - 1}", "share": len(rows) / len(res),
                          "predicted": sum(pd_of(r[0].score, cfg) for r in rows) / len(rows),
                          "observed": sum(r[1] for r in rows) / len(rows), "n": len(rows)})
    zones = {}
    for name, test in (("Отказ", lambda s: s < z1), ("Ручной разбор", lambda s: z1 <= s < z3),
                       ("Авто", lambda s: s >= z3)):
        rows = [r for r in res if test(r[0].score)]
        zones[name] = {"share": len(rows) / len(res), "dr": sum(r[1] for r in rows) / max(len(rows), 1)}
    dscr = {}
    if not any(math.isnan(r[2]) for r in res):
        kept = [r for r in res if r[0].score >= z1]
        for name, test in (("ниже 1,25", lambda d: d < 1.25), ("1,25 и выше", lambda d: d >= 1.25)):
            rows = [r for r in kept if test(r[2])]
            dscr[name] = {"share": len(rows) / max(len(kept), 1), "dr": sum(r[1] for r in rows) / max(len(rows), 1)}
    return {"gini": gini(scores, ys), "dr": sum(ys) / len(ys), "bands": table, "zones": zones, "dscr": dscr,
            "n": len(res)}


def by_group(data: list[tuple[ScoringInputs, int, float]]) -> dict[str, float]:
    def dr(rows):
        return sum(r[1] for r in rows) / max(len(rows), 1)
    return {"младше года": dr([r for r in data if r[0].business_months < 12]),
            "1–3 года": dr([r for r in data if 12 <= r[0].business_months < 36]),
            "3 года и старше": dr([r for r in data if r[0].business_months >= 36])}


# --- Отчёт ---------------------------------------------------------------------------------------------

def _pct(x: float) -> str:
    return f"{x * 100:.1f}".replace(".", ",") + "%"


def _num(v: int) -> str:
    return f"{v:,}".replace(",", " ")


def _pts(v: int) -> str:
    return "0" if v == 0 else f"{v:+d}".replace("-", "−")


def report(card: dict, train: dict, test: dict, groups: dict, n: int, source: str) -> str:
    p = card["points"]
    age = ["до 1 года", "1–2 года", "2–3 года", "3–5 лет", "5 лет и больше"]
    req = ["до 10%", "10–20%", "20–35%", "35% и больше"]
    load = ["до 1", "1–2", "2–3", "3–4", "4 и больше"]
    disc = {"clean": "без существенных просрочек", "minor_old": "6–30 дней, больше года назад",
            "minor_recent": "6–30 дней за последний год или больше двух раз", "serious": "31–90 дней",
            "default": "больше 90 дней"}
    lines = [
        "# Калибровка скоринговой карты",
        "",
        f"Версия карты **{card['version']}**. Файл создан скриптом `tools/calibrate.py` — не правьте руками, "
        "перезапустите скрипт.",
        "",
        "## Что откалибровано и как",
        "",
        "- **Баллы факторов и база** — логистической регрессией на портфеле с известными исходами: "
        f"{source}. Корзины факторов — та же функция, что в скоринге (`scoring.levels`).",
        "- **Шкала**: 60 баллов ↔ вероятность дефолта за год (PD) 5%, каждые 10 баллов — шансы дефолта "
        "вдвое ниже. Поэтому у любой оценки есть PD, а разница в баллах значит одно и то же по всей шкале.",
        f"- **Порог отказа** — {card['zones']['decline_below']} баллов: ниже ожидаемые потери PD × LGD без залога "
        f"(40%) больше {APPETITE['max_risk_premium'] * 100:.0f} п. п. — максимальной надбавки к ставке, PD выше "
        f"{_pct(APPETITE['max_risk_premium'] / 0.4)}.",
        f"- **Порог автоодобрения** — {card['zones']['auto_from']} баллов: PD не выше "
        f"{_pct(APPETITE['auto_max_pd'])}. Между порогами решает аналитик.",
        "- **Надбавка к ставке** = PD × LGD: залог снижает LGD и поэтому ставку, но не баллы — он уменьшает "
        "потери, а не вероятность дефолта.",
        "- **Не калибруется** (задано нормативами и экспертно): границы корзин, сроки просрочки по 590-П, порог "
        "существенности просрочки, дисконты и LGD залога по Базелю III, норма покрытия платежей 1,25.",
        "",
        "## Баллы",
        "",
        f"База **{card['base']}** — оценка компании «2 года, без поручительства, выручка без изменений, запрос до "
        "10% выручки, нагрузка до 1, просрочки 6–30 дней больше года назад, недоимки нет». Дальше плюсы и минусы:",
        "",
        "| Фактор | Корзина → баллы |",
        "|---|---|",
        "| Срок бизнеса | " + " · ".join(f"{a}: {_pts(v)}" for a, v in zip(age, p["business_age"])) + " |",
        "| Платёжная дисциплина | " + " · ".join(f"{disc[k]}: {_pts(p['payment_discipline'][k])}"
                                                for k in LEVELS["payment_discipline"]) + " |",
        f"| Поручительство собственника | есть: {_pts(p['guarantor'])} |",
        "| Динамика выручки | " + " · ".join(f"{n}: {_pts(p['revenue_trend'][k])}" for k, n in
                                            (("GROWTH", "рост"), ("FLAT", "без изменений"),
                                             ("DECLINE", "снижение"))) + " |",
        "| Запрос к выручке | " + " · ".join(f"{a}: {_pts(v)}" for a, v in zip(req, p["request_to_revenue"])) + " |",
        "| Долговая нагрузка | " + " · ".join(f"{a}: {_pts(v)}" for a, v in zip(load, p["debt_load"]))
        + f" · прибыль ≤ 0: {_pts(p['debt_load_no_profit'])} |",
        f"| Налоговая недоимка | до 100 тыс. ₽: {_pts(p['tax_arrears']['small'])} · больше: "
        f"{_pts(p['tax_arrears']['large'])} |",
        "",
        "## Проверка на отложенной выборке",
        "",
        f"Обучение — {_num(train['n'])} заявок, проверка — {_num(test['n'])} других заявок того же портфеля, "
        "тем же кодом, что считает оценку в продукте.",
        "",
        f"- **Gini** {test['gini']:.2f} (на обучении {train['gini']:.2f}): чем ближе к 1, тем лучше карта отделяет ".replace(".", ",")
        + "будущие дефолты. На реальных данных у карт для МСБ обычно 0,4–0,6: в синтетике меньше неизвестного, "
        "поэтому выше.",
        f"- Дефолтность портфеля за год — {_pct(test['dr'])}; по сроку работы: "
        + ", ".join(f"{k} — {_pct(v)}" for k, v in groups.items()) + ".",
        "",
        "| Баллы | Доля заявок | PD по шкале | Дефолтов на деле |",
        "|---|---|---|---|",
        *[f"| {b['band']} | {_pct(b['share'])} | {_pct(b['predicted'])} | {_pct(b['observed'])} |"
          for b in test["bands"]],
        "",
        "| Зона | Доля заявок | Дефолтов на деле |",
        "|---|---|---|",
        *[f"| {k} | {_pct(v['share'])} | {_pct(v['dr'])} |" for k, v in test["zones"].items()],
    ]
    if test["dscr"]:
        lines += [
            "",
            "Покрытие платежей (правило кредитной логики, не балл карты) среди заявок выше порога отказа:",
            "",
            "| Покрытие | Доля | Дефолтов на деле |",
            "|---|---|---|",
            *[f"| {k} | {_pct(v['share'])} | {_pct(v['dr'])} |" for k, v in test["dscr"].items()],
        ]
    problems = monotonic_problems(card)
    lines += ["", "Монотонность баллов: " + ("соблюдена — чем хуже корзина, тем меньше баллов." if not problems
                                            else "НАРУШЕНА: " + "; ".join(problems))]
    lines += [
        "",
        "## Синтетический портфель: допущения и опоры",
        "",
        "Истории дефолтов у нас нет, поэтому портфель синтетический. Дефолт в нём не выводится из баллов, "
        "а возникает из денежного потока и качества управления, — так регрессия восстанавливает веса, "
        "а не повторяет заданные. Числа ниже — допущения генератора и открытые данные, к которым они привязаны.",
        "",
        "| Что | Допущение | Опора |",
        "|---|---|---|",
        "| Уровень дефолтов | дефолтность всего потока заявок выше доли просрочки в портфеле банка: часть "
        "заявок отсекают стопы и аналитик; в одобряемой части (авто и ручной разбор) — порядка 4–5% | Доля "
        "просроченной задолженности в портфеле МСБ снизилась с 4,4 до 4% за 2025 год [4] |",
        "| Молодые компании | выше колебания прибыли и риск закрытия: дефолтность младше года — в 2–3 раза выше, "
        "чем у компаний старше 3 лет | Из компаний и ИП, зарегистрированных в 2022 году, к концу 2025-го работает "
        "половина [5] |",
        "| Дефолт | три платежа подряд не внесены — просрочка больше 90 дней | Определение дефолта: просрочка "
        "свыше 90 дней [3] |",
        "| Корзины просрочек | до 5 дней — техническая, 6–30 — небольшая, 31–90 — серьёзная, больше 90 — дефолт | "
        "Качество обслуживания долга юрлица: хорошее — до 5 дней, среднее — 6–30, неудовлетворительное — больше 30 [1] |",
        "| Существенность просрочки | больше 50 тыс. ₽ и больше 1% долга, иначе не учитываем | Порог EBA: "
        "500 евро и 1% [3] |",
        "| Потери при дефолте | без залога 40%; часть, покрытая залогом: недвижимость 20%, прочее 25%; дисконт к "
        "стоимости 40% | Базель III, CRE32.6 и CRE32.11 [2] |",
        "| Денежный поток | маржа EBITDA около 10% (у новых компаний около 5%) ± разброс; отчётность неточна "
        "(настоящая маржа отличается от заявленной на ±4 п. п.); помесячные колебания прибыли 25–45%, удачный или "
        "неудачный год ±20–30%; потеря ключевого клиента (маржа −6 п. п.) — у 5–21% компаний; подушка — 3% "
        "выручки и 2–5 месяцев прибыли; короткие кредиты частично рефинансируются | Допущение |",
        "| Качество управления | ненаблюдаемый фактор: чаще и тяжелее просрочки (свежие говорят больше давних), "
        "чаще недоимки, чаще закрытие бизнеса | Допущение |",
        "| Поручительство собственника | собственник закрывает один пропущенный платёж и реже бросает бизнес "
        "с долгами | Допущение |",
        "",
        "**Ограничение.** Это калибровка на синтетике: она делает веса согласованными между собой и с "
        "риск-аппетитом и показывает, что карта различает риск. Реальные PD появятся после калибровки на истории "
        "банка — тем же скриптом: `python tools/calibrate.py --data history.csv`.",
        "",
        "## Источники",
        "",
        *[f"{i}. {src}" for i, src in enumerate(SOURCES, 1)],
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", type=Path, help="CSV с историей заявок и колонкой default (0/1)")
    ap.add_argument("-n", type=int, default=200_000, help="размер синтетического портфеля")
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--dry-run", action="store_true", help="только показать результат, файлы не писать")
    args = ap.parse_args()

    if args.data:
        data, source = read_history(args.data), f"история банка ({args.data.name})"
    else:
        data, source = generate(args.n, args.seed), f"синтетический портфель из {_num(args.n)} заявок (seed {args.seed})"
    cut = len(data) * 2 // 3
    train, test = data[:cut], data[cut:]
    card = fit_card(train)
    tr, te = evaluate(card, train), evaluate(card, test)
    text = report(card, tr, te, by_group(data), len(data), source)
    print(json.dumps(card, ensure_ascii=False, indent=2))
    print("по возрасту:", {k: round(v, 3) for k, v in by_group(data).items()})
    print("диапазоны:", [(b["band"], round(b["share"], 2), round(b["predicted"], 3), round(b["observed"], 3))
                         for b in te["bands"]], "покрытие:", te["dscr"])
    print(f"Gini {te['gini']:.3f}, дефолтность {te['dr']:.3f}, зоны: "
          + ", ".join(f"{k} {v['share']:.2f}/{v['dr']:.3f}" for k, v in te["zones"].items()))
    if problems := monotonic_problems(card):
        print("ВНИМАНИЕ, немонотонно:", problems)
    if not args.dry_run:
        OUT.write_text(json.dumps(card, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        REPORT.write_text(text, encoding="utf-8")
        print(f"Записано: {OUT.relative_to(ROOT)}, {REPORT.relative_to(ROOT)}")


def fit_card(data: list[tuple[ScoringInputs, int, float]]) -> dict:
    return to_scorecard(monotone(*fit(data)))


if __name__ == "__main__":
    main()
