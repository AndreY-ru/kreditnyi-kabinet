"""Слой 3: кредитная логика продукта.

Одинаковая оценка может дать разные условия для разных продуктов. На вход: оценка, продукт,
сумма, срок, стопы. На выход: авто-одобрение, отказ или ручной разбор (с подсказкой для аналитика).
Ставка = базовая ставка продукта + ожидаемые потери PD × LGD: PD — из шкалы баллов, LGD — по Базелю III
(без залога 40%, часть, покрытая залогом с дисконтом, — 0–25% в зависимости от вида). Залог работает здесь,
а не в баллах: он не делает компанию надёжнее, а уменьшает потери — снижает ставку и поднимает лимит.

Здесь же проверки, которые зависят от продукта и срока, а не только от риска компании:
- покрытие платежей (DSCR): прибыль до % и налогов + амортизация должна покрывать платежи по всем
  кредитам с запасом. Долговая нагрузка в карте — про размер долга, а покрытие — про то, хватит ли денег
  каждый месяц: 5 млн на полгода и на три года — разные платежи;
- сверка анкеты с ФНС: выручка или прибыль в анкете заметно выше отчётности — авто-одобрения нет, сверяет
  аналитик. Прибыль завышать выгоднее всего: от неё зависят долговая нагрузка и покрытие платежей.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.models import Product
from app.services.scoring import DEFAULT_MODEL, ScoreOutcome, ScoringInputs, classify_overdues, pd_of, score


@dataclass
class CreditOutcome:
    status: str  # APPROVED_AUTO, DECLINED, MANUAL_REVIEW
    reason_codes: list[str] = field(default_factory=list)
    review_reasons: list[str] = field(default_factory=list)
    offer: dict[str, Any] | None = None
    hint: dict[str, Any] | None = None
    priority: int = 0
    metrics: dict[str, Any] = field(default_factory=dict)  # покрытие и платежи — в карточку аналитика


def _mln(v: int) -> str:
    return f"{v / 1_000_000:.1f}".replace(".0", "").replace(".", ",") + " млн ₽"


def credit_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    """Параметры кредитной логики; ключей, которых нет в старых версиях карты, берём по умолчанию."""
    return {**DEFAULT_MODEL["credit"], **cfg.get("credit", {})}


def collateral_info(inp: ScoringInputs, cfg: dict[str, Any]) -> dict[str, Any] | None:
    """Залог: вид, стоимость и стоимость с дисконтом — сколько банк выручит при продаже."""
    kind = credit_cfg(cfg)["collateral"].get(inp.collateral_kind or "")
    if not inp.has_collateral or kind is None or not inp.collateral_value:
        return None
    return {"kind": inp.collateral_kind, "name": kind["name"], "value": inp.collateral_value,
            "haircut": kind["haircut"], "cover": int(inp.collateral_value * (1 - kind["haircut"])), "lgd": kind["lgd"]}


def lgd_of(inp: ScoringInputs, cfg: dict[str, Any], amount: int | None = None) -> float:
    """LGD сделки (Базель III, CRE32.10): средневзвешенно по необеспеченной и покрытой залогом частям."""
    c = credit_cfg(cfg)
    amount = amount or inp.amount
    col = collateral_info(inp, cfg)
    if col is None or amount <= 0:
        return c["lgd_unsecured"]
    secured = min(col["cover"], amount)
    return (c["lgd_unsecured"] * (amount - secured) + col["lgd"] * secured) / amount


def rate_for(product: Product, score: int, cfg: dict[str, Any], lgd: float | None = None) -> Decimal:
    """Ставка = базовая ставка продукта + ожидаемые потери PD × LGD, в п. п. с округлением вверх до 0,1."""
    lgd = credit_cfg(cfg)["lgd_unsecured"] if lgd is None else lgd
    premium = math.ceil(pd_of(score, cfg) * lgd * 1000 - 1e-9) / 10
    return (Decimal(str(product.base_rate)) + Decimal(str(premium))).quantize(Decimal("0.01"))


def annuity(amount: int, rate: Decimal, months: int) -> int:
    r = float(rate) / 100 / 12
    if months <= 0:
        return amount
    if r == 0:
        return round(amount / months)
    payment = amount * r / (1 - (1 + r) ** -months)
    return int(Decimal(payment).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def existing_payment(inp: ScoringInputs, cfg: dict[str, Any]) -> int:
    """Платёж по текущим долгам в месяц: из БКИ или анкеты. Неизвестен — оцениваем по остатку долга."""
    if inp.existing_monthly:
        return inp.existing_monthly
    if not inp.existing_debt:
        return 0
    c = credit_cfg(cfg)
    return annuity(inp.existing_debt, Decimal(str(c["existing_rate"])), c["existing_term"])


def coverage(inp: ScoringInputs, out: ScoreOutcome, rate: Decimal, cfg: dict[str, Any]) -> float | None:
    """Покрытие платежей (DSCR) = (прибыль до % и налогов + амортизация) / платежи по всем кредитам за год."""
    if out.ebitda is None or out.ebitda <= 0:
        return None
    pay = annuity(inp.amount, rate, inp.term_months) + existing_payment(inp, cfg)
    return out.ebitda / (12 * pay) if pay else None


def _payment_cap(inp: ScoringInputs, out: ScoreOutcome, rate: Decimal, cfg: dict[str, Any], term: int) -> int:
    """Наибольшая сумма на этот срок, при которой покрытие не ниже нормы."""
    if out.ebitda is None or out.ebitda <= 0:
        return 0
    room = out.ebitda / 12 / credit_cfg(cfg)["min_dscr"] - existing_payment(inp, cfg)
    if room <= 0:
        return 0
    r = float(rate) / 100 / 12
    return int(room * term) if r == 0 else int(room * (1 - (1 + r) ** -term) / r)


def limits(inp: ScoringInputs, out: ScoreOutcome, product: Product, cfg: dict[str, Any],
           term: int | None = None) -> dict[str, int]:
    """Потолки суммы: продукт, долговая нагрузка, платежи на этот срок, лимит без залога."""
    c = credit_cfg(cfg)
    caps = {"product": product.max_amount}
    if out.ebitda is not None:
        caps["debt_load"] = int(c["max_debt_load"] * out.ebitda - inp.existing_debt) if out.ebitda > 0 else 0
        rate = rate_for(product, out.score, cfg, lgd_of(inp, cfg))
        caps["payments"] = _payment_cap(inp, out, rate, cfg, term or inp.term_months)
    if product.unsecured_max:  # сверх лимита без залога — только в пределах залога с дисконтом
        col = collateral_info(inp, cfg)
        caps["unsecured"] = product.unsecured_max + (col["cover"] if col else 0)
    return caps


def max_affordable(inp: ScoringInputs, out: ScoreOutcome, product: Product, cfg: dict[str, Any],
                   term: int | None = None) -> int:
    """Максимальная сумма, которая укладывается во все потолки, с округлением вниз."""
    step = credit_cfg(cfg)["round_to"]
    return max(min(limits(inp, out, product, cfg, term).values()) // step * step, 0)


def term_for(inp: ScoringInputs, out: ScoreOutcome, product: Product, cfg: dict[str, Any]) -> int | None:
    """Самый короткий срок дольше запрошенного, на котором вся сумма укладывается в потолки продукта."""
    for t in range(inp.term_months + 1, product.max_term + 1):
        if max_affordable(inp, out, product, cfg, t) >= inp.amount:
            return t
    return None


def serious_history(inp: ScoringInputs, cfg: dict[str, Any]) -> str | None:
    """Просрочка больше 90 дней за 2 года или больше 30 дней за последний год — решает аналитик."""
    pd = cfg["payment_discipline"]
    level, material, _ = classify_overdues(inp.bki_loans, cfg)
    if level == "default":
        return f"В кредитной истории просрочка больше {pd['default_days']} дней за 2 года"
    if any(o["days"] > pd["minor_days"] and o.get("months_ago", 0) < pd["recent_months"] for o in material):
        return f"В кредитной истории просрочка больше {pd['minor_days']} дней за последний год"
    return None


def revenue_gap(inp: ScoringInputs, cfg: dict[str, Any]) -> str | None:
    """Выручка в анкете заметно выше отчётности в ФНС. Молодым компаниям прощаем: полного года в ФНС ещё нет."""
    if inp.fns_revenue is None or not inp.revenue or (inp.business_months or 0) < 12:
        return None
    if inp.revenue <= inp.fns_revenue * (1 + credit_cfg(cfg)["revenue_gap"]):
        return None
    if inp.fns_revenue <= 0:
        return "В отчётности ФНС выручки нет, а в анкете есть"
    pct = round((inp.revenue / inp.fns_revenue - 1) * 100)
    return f"Выручка в анкете на {pct}% выше, чем в отчётности ФНС ({_mln(inp.fns_revenue)})"


def _pct(x: float) -> str:
    return f"{x * 100:.1f}".replace(".", ",").replace(",0", "") + "%"


def profit_gap(inp: ScoringInputs, cfg: dict[str, Any]) -> str | None:
    """Прибыль в анкете заметно выше отчётности ФНС. Сравниваем рентабельность, а не рубли: выручка могла вырасти.

    По ФНС «выручка − расходы» — прибыль до налогов уже после процентов, а в анкете — до процентов и налогов
    плюс амортизация, поэтому анкета честно бывает выше на несколько пунктов — отсюда запас profit_gap.
    """
    if (inp.fns_revenue is None or inp.fns_expenses is None or inp.fns_revenue <= 0 or not inp.revenue
            or inp.ebit is None or (inp.business_months or 0) < 12):
        return None
    declared = (inp.ebit + (inp.amortization or 0)) / inp.revenue
    reported = (inp.fns_revenue - inp.fns_expenses) / inp.fns_revenue
    if declared - reported <= credit_cfg(cfg)["profit_gap"]:
        return None
    if reported <= 0:
        return f"По отчётности ФНС за прошлый год убыток, а в анкете прибыль — {_pct(declared)} выручки"
    return f"Прибыль в анкете — {_pct(declared)} выручки, по отчётности ФНС — {_pct(reported)}"


def decide(product: Product, inp: ScoringInputs, out: ScoreOutcome, cfg: dict[str, Any]) -> CreditOutcome:
    # 1. Жёсткие стопы
    if out.knockouts:
        return CreditOutcome("DECLINED", reason_codes=sorted({k["reason"] for k in out.knockouts}))

    # 2. Низкая зона
    if out.zone == "LOW":
        negative = [f for f in out.factors if f["points"] < 0]
        negative.sort(key=lambda f: f["points"])
        codes = list(dict.fromkeys(f["reason"] for f in negative[:2])) or ["FINANCIALS"]
        return CreditOutcome("DECLINED", reason_codes=codes)

    # 3. Ручной разбор или авто-одобрение
    review: list[str] = []
    codes: list[str] = []
    priority = 0
    c = credit_cfg(cfg)
    lgd = lgd_of(inp, cfg)
    rate = rate_for(product, out.score, cfg, lgd)
    caps = limits(inp, out, product, cfg)
    cap = max_affordable(inp, out, product, cfg)
    dscr = coverage(inp, out, rate, cfg)
    col = collateral_info(inp, cfg)
    metrics = {"payment": annuity(inp.amount, rate, inp.term_months), "existing_payment": existing_payment(inp, cfg),
               "dscr": round(dscr, 2) if dscr is not None else None, "min_dscr": c["min_dscr"],
               "pd": round(pd_of(out.score, cfg), 4), "lgd": round(lgd, 3), "rate": str(rate),
               "collateral_cover": col["cover"] if col else None}

    if out.zone == "GRAY":
        review.append(f"Оценка {out.score} в серой зоне")
    if product.auto_limit == 0:
        review.append("Продукт рассматривается только специалистом" + (" (господдержка)" if product.gov_support else ""))
    elif inp.amount > product.auto_limit:
        review.append(f"Сумма выше авто-лимита продукта ({product.auto_limit // 1_000_000} млн ₽)")
    if inp.tax_arrears >= c["manual_tax_arrears_from"]:
        review.append(f"Налоговая недоимка {inp.tax_arrears:,} ₽".replace(",", " "))
        codes.append("TAX_ARREARS")
        priority += 2
    if history := serious_history(inp, cfg):  # политика: такие истории автоматом не одобряем, даже при высоких баллах
        review.append(f"{history} — без автоодобрения")
        codes.append("PAYMENT_HISTORY")
        priority += 1
    if gap := revenue_gap(inp, cfg):
        review.append(f"{gap} — сверить по ОСВ 62 или выписке")
        codes.append("FINANCIALS")
        priority += 1
    if gap := profit_gap(inp, cfg):
        review.append(f"{gap} — сверить по бухгалтерской отчётности")
        codes.append("FINANCIALS")
        priority += 1
    if inp.amount > cap:
        if inp.amount > caps.get("unsecured", inp.amount):
            review.append(f"Без залога доступно не больше {_mln(product.unsecured_max)}"
                          + (f", залог с дисконтом добавляет {_mln(col['cover'])}" if col else ""))
            codes.append("COLLATERAL")
        if inp.amount > caps.get("debt_load", inp.amount):
            review.append(f"Долг с новым кредитом больше {c['max_debt_load']:g} годовых прибылей".replace(".", ","))
            codes.append("DEBT_LOAD")
        if inp.amount > caps.get("payments", inp.amount):
            share = f"{round(100 / dscr)}%" if dscr else "больше всей"
            review.append(f"Платежи по всем кредитам — {share} прибыли до % и налогов: покрытие "
                          f"{dscr or 0:.2f}× при норме {c['min_dscr']:.2f}×".replace(".", ","))
            codes.append("DEBT_LOAD")
        priority += 1

    if not review:
        return CreditOutcome(
            "APPROVED_AUTO",
            offer={"amount": inp.amount, "term_months": inp.term_months, "rate": rate,
                   "monthly_payment": metrics["payment"]},
            metrics=metrics,
        )

    hint = None
    if inp.amount > cap:
        longer = term_for(inp, out, product, cfg)
        if cap >= product.min_amount:
            hint = {"amount": cap, "term_months": inp.term_months, "rate": str(rate),
                    "text": f"При текущей нагрузке допустимо до {_mln(cap)} на {inp.term_months} мес."
                            + (f", или вся сумма на {longer} мес." if longer else "")}
        elif longer:
            hint = {"amount": inp.amount, "term_months": longer, "rate": str(rate),
                    "text": f"Вся сумма укладывается в нагрузку при сроке от {longer} мес."}
    else:
        hint = {"amount": inp.amount, "term_months": inp.term_months, "rate": str(rate),
                "text": "Запрошенная сумма укладывается в допустимую нагрузку"}
    return CreditOutcome("MANUAL_REVIEW", reason_codes=list(dict.fromkeys(codes)), review_reasons=review, hint=hint,
                         priority=priority, metrics=metrics)


def config_problem(cfg: dict[str, Any]) -> str | None:
    """Проверка новой версии карты до включения: пробная заявка проходит скоринг и кредитную логику.

    Конфигурацию присылает администратор через API; сломанная версия уронила бы конвейер у всех заявок.
    None — всё в порядке, иначе — что не так.
    """
    probe = ScoringInputs(
        amount=1_000_000, term_months=12, business_months=48, company_status="ACTIVE", min_business_months=0,
        revenue=50_000_000, ebit=5_000_000, amortization=500_000, revenue_trend="GROWTH", existing_debt=1_000_000,
        tax_arrears=50_000, has_collateral=True, has_guarantor=True,
        bki_loans=[{"balance": 1_000_000, "overdues_24m": [{"days": 20, "amount": 60_000, "months_ago": 3}]}],
        fns_revenue=50_000_000, collateral_kind="REAL_ESTATE", collateral_value=2_000_000)
    product = Product(code="PROBE", name="Проверка", kind="LOAN", goals=["working"], min_amount=100_000,
                      max_amount=10_000_000, min_term=1, max_term=60, min_business_months=0,
                      requires_collateral=False, unsecured_max=5_000_000, requires_guarantor=False,
                      base_rate=Decimal("18"), auto_limit=3_000_000, decision_days=2, gov_support=False, active=True)
    try:
        zones = cfg["zones"]
        if not 0 < zones["decline_below"] <= zones["auto_from"] <= 100:
            return "зоны: нужно 0 < decline_below ≤ auto_from ≤ 100"
        decide(product, probe, score(probe, cfg), cfg)
    except Exception as exc:  # noqa: BLE001 — любая поломка конфигурации — отказ с понятной причиной
        return f"{type(exc).__name__}: {exc}"
    return None



def activate_model(db: Any, user: Any, version: str) -> None:
    """Сделать активной другую версию карты (например, откатиться). Перед включением — пробный расчёт."""
    from sqlalchemy import select

    from app.domain.transitions import NotFound, ValidationFailed
    from app.models import AuditLog, ScoringModel

    target = db.scalar(select(ScoringModel).where(ScoringModel.version == version))
    if target is None:
        raise NotFound("Такой версии карты нет")
    problem = config_problem(target.config)
    if problem:
        raise ValidationFailed(f"Версия {version} не работает с текущим кодом: {problem}", code="model_broken")
    previous = [m.version for m in db.scalars(select(ScoringModel).where(ScoringModel.active.is_(True)))]
    for m in db.scalars(select(ScoringModel)):
        m.active = m.id == target.id
    db.add(AuditLog(actor_id=user.id, action="scoring.activate", entity="scoring_model", entity_id=target.id,
                    data={"v": version, "was": ", ".join(previous)}))
    db.flush()
