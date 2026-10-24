"""Распознавание ОСВ по счёту 62 (выгрузка 1С) и сверка с выручкой из заявки."""
from __future__ import annotations

import io

import pytest
from openpyxl import Workbook

from app.services import documents
from tests.helpers import SV, consent_all, draft, submit_and_process
from tests.test_web import web_login

DECLARED = 84_600_000  # выручка «Северного ветра» в заявке, без НДС


def osv_xlsx(debit: float, *, total_label: str = "Итого") -> bytes:
    """ОСВ по счёту 62 в том виде, как её выгружает 1С."""
    wb = Workbook()
    ws = wb.active
    ws.append(["ООО «Северный ветер»"])
    ws.append(["Оборотно-сальдовая ведомость по счету 62 за 01.10.2025 - 30.09.2026"])
    ws.append([])
    ws.append(["Счет", "Сальдо на начало периода", None, "Обороты за период", None, "Сальдо на конец периода", None])
    ws.append([None, "Дебет", "Кредит", "Дебет", "Кредит", "Дебет", "Кредит"])
    ws.append(["62.01", 8_450_000, None, debit, debit - 1_650_000, 10_100_000, None])
    ws.append([total_label, 8_450_000, None, debit, debit - 1_650_000, 10_100_000, None])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def check_bytes(tmp_path, content: bytes, name: str = "osv62.xlsx", declared: int | None = DECLARED):
    p = tmp_path / name
    p.write_bytes(content)
    return documents.check(documents.OSV_62, name, str(p), "sha-" + str(len(content)) + name, declared)


def test_osv_matches_declared_revenue_with_vat(tmp_path):
    c = check_bytes(tmp_path, osv_xlsx(101_520_000))  # 84,6 млн × 1,2
    assert c.recognized and c.debit == 101_520_000 and c.period == "01.10.2025 – 30.09.2026"
    assert c.verdict == "match" and c.with_vat and c.diff_pct == 0


def test_osv_mismatch_is_flagged(tmp_path):
    c = check_bytes(tmp_path, osv_xlsx(70_000_000))
    assert c.verdict == "mismatch" and c.diff_pct == 17


def test_osv_csv_from_1c_in_cp1251(tmp_path):
    text = ("Оборотно-сальдовая ведомость по счету 62 за 01.10.2025 - 30.09.2026\n"
            "Счет;Сальдо на начало периода;;Обороты за период;;Сальдо на конец периода;\n"
            ";Дебет;Кредит;Дебет;Кредит;Дебет;Кредит\n"
            "62;8 450 000,00;;101 520 000,00;99 870 000,00;10 100 000,00;\n")
    c = check_bytes(tmp_path, text.encode("cp1251"), name="osv.csv")
    assert c.recognized and c.debit == 101_520_000 and c.verdict == "match"


@pytest.mark.parametrize("content, name, problem", [
    (b"%PDF-1.4 scan", "scan.pdf", "XLSX и CSV"),
    (b"not an xlsx", "broken.xlsx", "не читается"),
])
def test_osv_not_recognized_is_reported_not_crashing(tmp_path, content, name, problem):
    c = check_bytes(tmp_path, content, name=name)
    assert not c.recognized and problem in c.problem and c.verdict == "unknown"


def test_other_documents_are_not_checked(tmp_path):
    p = tmp_path / "x.xlsx"
    p.write_bytes(osv_xlsx(1))
    assert documents.check("Договор с ключевым покупателем", "x.xlsx", str(p), "s", DECLARED) is None


def test_analyst_sees_recognition_on_card(anna, maria):
    app = draft(anna, SV, "WORKING", 5_000_000, 24)
    consent_all(anna, app["id"])
    submit_and_process(anna, app["id"])
    v = maria.get(f"/api/bank/applications/{app['id']}").json()["application"]["version"]
    maria.post(f"/api/bank/applications/{app['id']}/take")
    r = maria.post(f"/api/bank/applications/{app['id']}/decision", json={
        "kind": "info", "expected_version": v, "comment": "нужна ОСВ", "doc_kind": documents.OSV_62})
    assert r.status_code == 200, r.text
    r = anna.post(f"/api/applications/{app['id']}/info-response", data={"comment": "прикладываю"},
                  files={"files": ("osv62.xlsx", osv_xlsx(70_000_000),
                                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert r.status_code == 200, r.text
    page = web_login("maria@bank.local").get(f"/bank/applications/{app['id']}").text.replace(" ", " ")
    assert "Распознано" in page and "70 000 000" in page and "расхождение 17" in page  # суммы — с неразрывным пробелом
