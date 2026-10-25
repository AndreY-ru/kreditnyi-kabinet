"""Тестовые данные источников (только для демо): кредитная история в БКИ и сведения ФНС.

Банк не может править настоящие БКИ и ФНС — это смоделированные источники, и правки хранятся у самих моков
(служебный API /admin, файлы на томе mockdata). Приложение только вызывает этот API со страницы
«Тестовые данные» администратора и показывает, как скоринг прочитает новую историю.

Правка действует на будущие обращения к источнику: новая заявка компании получит новый отчёт БКИ,
«Проверка шансов» — новые сведения ФНС. Уже рассмотренные заявки хранят ответ, полученный тогда
(external_checks), — решение можно воспроизвести.
"""
from __future__ import annotations

import copy
import re
from typing import Any

from app.services.credit_logic import serious_history
from app.services.scoring import DISCIPLINE_LABELS, ScoringInputs, classify_overdues

KINDS = ["кредит", "лизинг", "овердрафт", "гарантия"]
STATUSES = {"ACTIVE": "Действует", "LIQUIDATING": "В процессе ликвидации", "REORGANIZING": "Реорганизация"}
PRESETS = {
    "clean": "Чистая история",
    "minor_old": "Небольшая просрочка больше года назад",
    "serious_recent": "Просрочка 45 дней в этом году",
    "default_past": "Просрочка больше 90 дней в прошлом",
    "current": "Действующая просрочка",
}
_MATURITY = re.compile(r"^(0[1-9]|1[0-2])\.\d{4}$")
_EXAMPLE_LOAN = {"creditor": "Банк Б", "kind": "кредит", "balance": 1_000_000, "monthly_payment": 50_000,
                 "maturity": "12.2027", "current_overdue_days": 0, "current_overdue_amount": 0, "overdues_24m": []}


def apply_preset(loans: list[dict[str, Any]], preset: str) -> list[dict[str, Any]]:
    """Готовый сценарий: убираем все просрочки и добавляем одну нужную к первому кредиту."""
    loans = copy.deepcopy(loans) or [dict(_EXAMPLE_LOAN)]
    for loan in loans:
        loan.update(overdues_24m=[], current_overdue_days=0, current_overdue_amount=0)
    first = loans[0]
    amount = max(60_000, int(first.get("monthly_payment") or 0))  # заведомо существенная: больше 50 тыс. и 1% долга
    episode = {"minor_old": (15, 14), "serious_recent": (45, 4), "default_past": (120, 15), "current": (45, 0)}
    if preset in episode:
        days, ago = episode[preset]
        first["overdues_24m"] = [{"days": days, "amount": amount, "months_ago": ago}]
    if preset == "current":
        first.update(current_overdue_days=45, current_overdue_amount=amount)
    return loans


def preview(loans: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any]:
    """Как скоринг прочитает эту историю: уровень дисциплины, баллы, стоп-фактор, политика автоодобрения."""
    level, material, technical = classify_overdues(loans, cfg)
    ko = cfg["knockouts"]
    knockout = any(l.get("current_overdue_days", 0) > ko["active_overdue_days"]
                   and l.get("current_overdue_amount", 0) > ko["active_overdue_amount"] for l in loans)
    probe = ScoringInputs(amount=0, term_months=12, business_months=None, company_status="ACTIVE",
                          min_business_months=0, revenue=None, ebit=None, amortization=None, revenue_trend=None,
                          existing_debt=0, tax_arrears=0, has_collateral=False, has_guarantor=False, bki_loans=loans)
    return {"level": level, "label": DISCIPLINE_LABELS[level], "points": cfg["payment_discipline"]["points"][level],
            "material": len(material), "technical": len(technical), "knockout": knockout,
            "no_auto": serious_history(probe, cfg),
            "balance": sum(int(l.get("balance") or 0) for l in loans),
            "monthly": sum(int(l.get("monthly_payment") or 0) for l in loans)}


def _int(value: str, field: str, errors: list[str], lo: int = 0, hi: int = 10**13) -> int:
    digits = re.sub(r"[^\d]", "", value or "")[:16]
    n = int(digits) if digits else 0
    if not lo <= n <= hi:
        errors.append(f"{field}: от {lo} до {hi:,}".replace(",", " "))
    return min(max(n, lo), hi)


def parse_loans(form: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Кредиты из формы: l{i}_поле, история — l{i}_o{j}_поле. Пустые и отмеченные «удалить» строки пропускаем."""
    loans, errors = [], []
    for i in range(int(form.get("n") or 0)):
        def g(key: str) -> str:
            return str(form.get(f"l{i}_{key}") or "").strip()
        if g("delete") or (not g("creditor") and not g("balance")):
            continue
        name = g("creditor")[:120] or "Без названия"
        history = []
        for j in range(int(form.get(f"l{i}_n") or 0)):
            def h(key: str) -> str:
                return str(form.get(f"l{i}_o{j}_{key}") or "").strip()
            if h("delete") or not h("days"):
                continue
            history.append({"days": _int(h("days"), f"{name}: дней просрочки", errors, 1, 3650),
                            "amount": _int(h("amount"), f"{name}: сумма просрочки", errors),
                            "months_ago": _int(h("ago"), f"{name}: сколько месяцев назад", errors, 0, 23)})
        maturity = g("maturity") or None
        if maturity and not _MATURITY.match(maturity):
            errors.append(f"{name}: погашение — в формате ММ.ГГГГ")
            maturity = None
        loans.append({"creditor": name, "kind": g("kind") if g("kind") in KINDS else "кредит",
                      "balance": _int(g("balance"), f"{name}: остаток", errors),
                      "monthly_payment": _int(g("monthly"), f"{name}: платёж", errors),
                      "maturity": maturity,
                      "current_overdue_days": _int(g("cur_days"), f"{name}: действующая просрочка, дней", errors, 0, 3650),
                      "current_overdue_amount": _int(g("cur_amount"), f"{name}: действующая просрочка, сумма", errors),
                      "overdues_24m": history})
    if len(loans) > 30:
        errors.append("Не больше 30 кредитов")
    return loans, errors


def parse_fns(form: Any) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    fields: dict[str, Any] = {}
    if form.get("status") in STATUSES:
        fields["status"] = form.get("status")
    reg = str(form.get("registered_at") or "").strip()
    if reg:
        if re.match(r"^\d{4}-\d{2}-\d{2}$", reg):
            fields["registered_at"] = reg
        else:
            errors.append("Дата регистрации — в формате ГГГГ-ММ-ДД")
    for key, label in (("tax_arrears", "Недоимка"), ("revenue_last_year", "Выручка"),
                       ("expenses_last_year", "Расходы"), ("employees", "Сотрудники")):
        raw = str(form.get(key) or "").strip()
        if raw:
            fields[key] = _int(raw, label, errors, 0, 1_000_000 if key == "employees" else 10**15)
    return fields, errors


def parse_new_company(form: Any) -> tuple[dict[str, Any], list[str]]:
    """Новая синтетическая компания из формы. Название дополняем формой собственности, если её нет."""
    errors: list[str] = []
    kind = "IP" if form.get("kind") == "IP" else "OOO"
    inn = re.sub(r"\D", "", str(form.get("inn") or ""))
    if inn:
        if len(inn) not in (10, 12):
            errors.append("ИНН — 10 цифр для организации или 12 для ИП; можно оставить пустым")
        else:
            kind = "OOO" if len(inn) == 10 else "IP"
    name = " ".join(str(form.get("name") or "").split())[:180]
    if len(name) < 2:
        errors.append("Укажите название")
    elif kind == "OOO" and not name.upper().startswith(("ООО", "АО", "ПАО", "ЗАО")):
        bare = name.strip("«»\"")
        name = f"ООО «{bare}»"
    elif kind == "IP" and not name.upper().startswith("ИП"):
        name = f"ИП {name}"
    reg = str(form.get("registered_at") or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", reg):
        errors.append("Дата регистрации — в формате ГГГГ-ММ-ДД")
    okved = str(form.get("okved") or "46.90").strip()
    if not re.match(r"^\d{2}(\.\d{1,2}){0,2}$", okved):
        errors.append("ОКВЭД — например 46.90")
    data: dict[str, Any] = {"kind": kind, "name": name, "registered_at": reg, "okved": okved,
                            "okved_name": " ".join(str(form.get("okved_name") or "").split())[:200]
                            or "Торговля оптовая неспециализированная",
                            "director": " ".join(str(form.get("director") or "").split())[:120],
                            "status": form.get("status") if form.get("status") in STATUSES else "ACTIVE"}
    if inn:
        data["inn"] = inn
    for key, label, hi in (("revenue_last_year", "Выручка", 10**15), ("expenses_last_year", "Расходы", 10**15),
                           ("employees", "Сотрудники", 1_000_000), ("tax_arrears", "Недоимка", 10**12)):
        raw = str(form.get(key) or "").strip()
        if raw:
            data[key] = _int(raw, label, errors, 0, hi)
    return data, errors
