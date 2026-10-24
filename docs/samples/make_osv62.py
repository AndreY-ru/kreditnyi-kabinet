"""Синтетические ОСВ по счёту 62 в формате выгрузки 1С — для показа распознавания документов.

Запуск из корня репозитория: python docs/samples/make_osv62.py
- osv62_severny_veter.xlsx — сходится с выручкой «Северного ветра» в заявке (84,6 млн без НДС = 101,52 млн с НДС);
- osv62_rashozhdenie.xlsx — обороты 70 млн: расхождение 17 % даже с учётом НДС.
Как показать: Мария запрашивает «Оборотно-сальдовая ведомость по счёту 62», Анна загружает файл,
в карточке заявки у Марии появляется «Распознано: …». Все цифры выдуманы.
"""
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font


def make(path: Path, debit: int) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "ОСВ 62"
    ws.append(["ООО «Северный ветер» (синтетические данные)"])
    ws.append(["Оборотно-сальдовая ведомость по счету 62 за 01.10.2025 - 30.09.2026"])
    ws.append([])
    ws.append(["Счет", "Сальдо на начало периода", None, "Обороты за период", None, "Сальдо на конец периода", None])
    ws.append([None, "Дебет", "Кредит", "Дебет", "Кредит", "Дебет", "Кредит"])
    buyers = [("ООО «Торговый дом Север»", 0.46), ("ООО «Сеть Магнолия»", 0.31), ("ИП Ковалёв А. В.", 0.23)]
    for name, share in buyers:
        d = round(debit * share)
        ws.append([f"62.01 {name}", round(8_450_000 * share), None, d, round((debit - 1_650_000) * share),
                   round(10_100_000 * share), None])
    ws.append(["Итого", 8_450_000, None, debit, debit - 1_650_000, 10_100_000, None])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
    ws.column_dimensions["A"].width = 40
    for col in "BCDEFG":
        ws.column_dimensions[col].width = 18
    wb.save(path)


if __name__ == "__main__":
    here = Path(__file__).parent
    make(here / "osv62_severny_veter.xlsx", 101_520_000)
    make(here / "osv62_rashozhdenie.xlsx", 70_000_000)
    print("готово:", *sorted(p.name for p in here.glob("osv62_*.xlsx")))
