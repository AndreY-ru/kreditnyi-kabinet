from __future__ import annotations

from fastapi.testclient import TestClient

from app import db as dbm
from app.worker import run_pending

SV = "7701234560"  # ООО «Северный ветер»: стабильная компания
YOUNG = "7801555014"  # ООО «Молодой старт»: 4 месяца
OVERDUE = "5001777021"  # ООО «Долговой центр»: действующая просрочка в БКИ
FNS_DOWN = "6022333043"  # ООО «Сбой связи»: «Прозрачный бизнес» недоступен
LIQUIDATING = "4703444053"  # ООО «Закат»: ликвидация

GOOD_FIN = {"revenue": 84_600_000, "expenses": 76_900_000, "ebit": 6_200_000, "amortization": 300_000,
            "revenue_trend": "GROWTH",
            "liabilities": [{"creditor": "Банк А", "balance": 2_400_000, "monthly_payment": 160_000, "maturity": "08.2027"},
                            {"creditor": "Лизинг", "balance": 1_100_000, "monthly_payment": 58_000, "maturity": "03.2028"}]}


def company_id(c: TestClient, inn: str) -> int:
    for co in c.get("/api/companies").json():
        if co["inn"] == inn:
            return co["id"]
    raise AssertionError(f"Компания {inn} не найдена у пользователя")


def product_id(c: TestClient, code: str) -> int:
    for p in c.get("/api/products").json():
        if p["code"] == code:
            return p["id"]
    raise AssertionError(code)


def draft(c: TestClient, inn: str, code: str, amount: int, term: int, *, goal: str = "working",
          guarantor: bool = True, collateral: bool = False, fin: dict | None = None) -> dict:
    r = c.post("/api/applications", json={"company_id": company_id(c, inn), "product_id": product_id(c, code),
                                          "amount": amount, "term_months": term, "goal": goal,
                                          "has_guarantor": guarantor, "has_collateral": collateral})
    assert r.status_code == 201, r.text
    app = r.json()
    r = c.put(f"/api/applications/{app['id']}/financials", json=fin or GOOD_FIN)
    assert r.status_code == 200, r.text
    return r.json()


def consent_all(c: TestClient, app_id: int, guarantor: bool = True) -> None:
    for kind in ["PD", "BKI"] + (["BKI_GUARANTOR"] if guarantor else []):
        r = c.post(f"/api/applications/{app_id}/consents", json={"kind": kind})
        assert r.status_code == 201, r.text


def submit_and_process(c: TestClient, app_id: int, key: str | None = None) -> dict:
    headers = {"Idempotency-Key": key} if key else {}
    r = c.post(f"/api/applications/{app_id}/submit", headers=headers)
    assert r.status_code == 200, r.text
    process()
    return c.get(f"/api/applications/{app_id}").json()


def process() -> int:
    with dbm.SessionLocal() as s:
        return run_pending(s)


def statuses(c: TestClient, app_id: int) -> list[str]:
    return [h["to_status"] for h in c.get(f"/api/applications/{app_id}/history").json()]
