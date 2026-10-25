"""Jinja2: шаблоны и фильтры форматирования (деньги, даты в часовом поясе отображения)."""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

from app.config import settings
from app.domain.states import BANK_LABELS, CLIENT_LABELS
from app.services.catalog import GOAL_PURPOSE, GOALS
from app.services.scoring import DEFAULT_MODEL, pd_of

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
STATIC = Path(__file__).parent / "static"
# Метка версии — хеш содержимого: после обновления браузер не возьмёт старые стили и скрипты из кеша
ASSETS = {name: f"/static/{name}?v={hashlib.sha256((STATIC / name).read_bytes()).hexdigest()[:10]}"
          for name in ("app.css", "app.js")}
TZ = ZoneInfo(settings.display_tz)


def _local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ)


def money(v: int | float | Decimal | None, suffix: str = " ₽") -> str:
    if v is None:
        return "—"
    return f"{int(v):,}".replace(",", " ") + suffix


def money_input(v: int | str | None) -> str:
    if isinstance(v, str):  # форма вернулась с ошибкой — показываем то, что ввели
        digits = re.sub(r"[^\d]", "", v)[:16]
        if not digits:
            return v
        v = (-1 if v.strip().startswith("-") else 1) * int(digits)
    return "" if v is None else f"{int(v):,}".replace(",", " ")


def mln(v: int | None) -> str:
    if v is None:
        return "—"
    if abs(v) >= 1_000_000:
        s = f"{v / 1_000_000:.1f}".replace(".0", "").replace(".", ",")
        return f"{s} млн ₽"
    return money(v)


def dt(v: datetime | None, fmt: str = "%d.%m.%Y, %H:%M") -> str:
    lv = _local(v)
    return lv.strftime(fmt) if lv else "—"


def dshort(v: datetime | date | None) -> str:
    if v is None:
        return "—"
    if isinstance(v, datetime):
        v = _local(v)
        return v.strftime("%d.%m, %H:%M")
    return v.strftime("%d.%m.%Y")


def ddate(v: datetime | date | None) -> str:
    if v is None:
        return "—"
    if isinstance(v, datetime):
        v = _local(v)
    return v.strftime("%d.%m.%Y")


def rate(v: Decimal | float | str | None) -> str:
    if v is None:
        return "—"
    s = f"{Decimal(str(v)):.2f}".rstrip("0").rstrip(".")
    return s.replace(".", ",")


def years_since(d: date | None) -> str:
    if not d:
        return "—"
    today = date.today()
    months = (today.year - d.year) * 12 + today.month - d.month - (1 if today.day < d.day else 0)
    y = months // 12
    if y == 0:
        return f"{months} мес."
    if y % 10 == 1 and y % 100 != 11:
        return f"{y} год"
    if y % 10 in (2, 3, 4) and y % 100 not in (12, 13, 14):
        return f"{y} года"
    return f"{y} лет"


def days_left(v: datetime | None) -> int | None:
    if v is None:
        return None
    lv = _local(v)
    return max(0, (lv.date() - datetime.now(TZ).date()).days)  # не «осталось −1 дней», пока воркер не закрыл


def hours_left(v: datetime | None) -> float | None:
    if v is None:
        return None
    lv = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    return (lv - datetime.now(timezone.utc)).total_seconds() / 3600


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many


env = templates.env
env.filters.update(money=money, money_input=money_input, mln=mln, dt=dt, dshort=dshort, ddate=ddate, rate=rate,
                   years_since=years_since)
env.globals.update(GOALS=GOALS, GOAL_PURPOSE=GOAL_PURPOSE, days_left=days_left, hours_left=hours_left,
                   plural=plural, COLLATERAL=DEFAULT_MODEL["credit"]["collateral"], pd_of=pd_of,
                   COLLATERAL_SHORT={"REAL_ESTATE": "Недвижимость", "VEHICLE": "Транспорт", "EQUIPMENT": "Оборудование",
                                     "GOODS": "Товары", "DEPOSIT": "Депозит"},
                   asset=ASSETS.__getitem__)

env.tests["search"] = lambda value, pattern: re.search(pattern, str(value)) is not None
env.globals["label_of"] = lambda status: BANK_LABELS.get(status, (status, "neutral"))
env.globals["client_label_of"] = lambda status: CLIENT_LABELS.get(status, (status, "neutral"))
