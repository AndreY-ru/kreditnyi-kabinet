"""Синтетические данные для моков ФНС и БКИ.

ВСЕ КОМПАНИИ И ЛЮДИ ВЫМЫШЛЕНЫ. Совпадения с реальными организациями случайны.
ИНН сгенерированы с корректной контрольной суммой, но не принадлежат реальным лицам.
"""
from __future__ import annotations

import hashlib
from datetime import date, timedelta


def _months_ago(months: int) -> str:
    d = date.today() - timedelta(days=int(months * 30.4))
    return d.isoformat()


def inn_is_valid(inn: str) -> bool:
    if not inn.isdigit() or len(inn) not in (10, 12):
        return False
    d = [int(c) for c in inn]
    if len(inn) == 10:
        k = [2, 4, 10, 3, 5, 9, 4, 6, 8]
        return sum(a * b for a, b in zip(d, k)) % 11 % 10 == d[9]
    k1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    k2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    return (
        sum(a * b for a, b in zip(d, k1)) % 11 % 10 == d[10]
        and sum(a * b for a, b in zip(d, k2)) % 11 % 10 == d[11]
    )


# ИНН, на котором мок ФНС всегда отвечает 503 (сценарий T8)
FNS_ALWAYS_DOWN = {"6022333043"}


def _loan(creditor, balance, monthly, maturity, overdue_days=0, overdue_amount=0, history=None, kind="кредит"):
    return {
        "creditor": creditor,
        "kind": kind,
        "balance": balance,
        "monthly_payment": monthly,
        "maturity": maturity,
        "current_overdue_days": overdue_days,
        "current_overdue_amount": overdue_amount,
        "overdues_24m": history or [],
    }


COMPANIES: dict[str, dict] = {
    "7701234560": {
        "egrul": {
            "kind": "OOO", "ogrn": "1197746123456", "name": "ООО «Северный ветер»",
            "registered_at": "2019-03-12", "status": "ACTIVE",
            "address": "г. Москва, ул. Примерная, д. 1", "okved": "46.90",
            "okved_name": "Торговля оптовая неспециализированная",
            "director": "Смирнов Игорь Олегович",
            "founders": [{"name": "Смирнов И. О.", "share": 100}],
        },
        "transparent": {"employees": 24, "tax_arrears": 0, "taxes_paid_last_year": 4_120_000,
                        "revenue_last_year": 84_600_000, "expenses_last_year": 76_900_000},
        "bki": [
            _loan("Банк А", 2_400_000, 160_000, "08.2027"),
            _loan("Лизинговая компания", 1_100_000, 58_000, "03.2028", kind="лизинг"),
        ],
    },
    "5401987658": {
        "egrul": {
            "kind": "OOO", "ogrn": "1155476098765", "name": "ООО «Пекарня на Садовой»",
            "registered_at": "2015-06-03", "status": "ACTIVE",
            "address": "г. Новосибирск, ул. Садовая, д. 12", "okved": "10.71",
            "okved_name": "Производство хлеба и мучных кондитерских изделий",
            "director": "Орлова Мария Сергеевна",
            "founders": [{"name": "Орлова М. С.", "share": 60}, {"name": "Орлов П. А.", "share": 40}],
        },
        "transparent": {"employees": 31, "tax_arrears": 312_000, "taxes_paid_last_year": 2_300_000,
                        "revenue_last_year": 41_000_000, "expenses_last_year": 37_500_000},
        "bki": [_loan("Банк Б", 900_000, 75_000, "12.2026", history=[{"days": 7, "amount": 75_000, "months_ago": 14}])],
    },
    "7724112230": {
        "egrul": {
            "kind": "OOO", "ogrn": "1102724011223", "name": "ООО «Технопарк Восток»",
            "registered_at": "2010-09-21", "status": "ACTIVE",
            "address": "г. Хабаровск, ул. Восточная, д. 5", "okved": "26.51",
            "okved_name": "Производство контрольно-измерительных приборов",
            "director": "Ким Денис Вячеславович",
            "founders": [{"name": "Ким Д. В.", "share": 100}],
        },
        "transparent": {"employees": 140, "tax_arrears": 0, "taxes_paid_last_year": 21_000_000,
                        "revenue_last_year": 260_000_000, "expenses_last_year": 231_000_000},
        "bki": [_loan("Банк В", 14_000_000, 690_000, "05.2028")],
    },
    "6658445560": {
        "egrul": {
            "kind": "OOO", "ogrn": "1146658044556", "name": "ООО «СтройМонтаж»",
            "registered_at": "2014-02-17", "status": "ACTIVE",
            "address": "г. Екатеринбург, ул. Строителей, д. 9", "okved": "43.21",
            "okved_name": "Производство электромонтажных работ",
            "director": "Захаров Олег Ильич",
            "founders": [{"name": "Захаров О. И.", "share": 100}],
        },
        "transparent": {"employees": 58, "tax_arrears": 0, "taxes_paid_last_year": 6_800_000,
                        "revenue_last_year": 96_000_000, "expenses_last_year": 88_000_000},
        "bki": [_loan("Банк Г", 3_000_000, 140_000, "11.2027")],
    },
    "7801555014": {
        "egrul": {
            "kind": "OOO", "ogrn": "1267800155501", "name": "ООО «Молодой старт»",
            "registered_at": _months_ago(4), "status": "ACTIVE",
            "address": "г. Санкт-Петербург, Невский пр., д. 100", "okved": "62.01",
            "okved_name": "Разработка компьютерного программного обеспечения",
            "director": "Лебедева Ольга Павловна",
            "founders": [{"name": "Лебедева О. П.", "share": 100}],
        },
        "transparent": {"employees": 4, "tax_arrears": 0, "taxes_paid_last_year": 0,
                        "revenue_last_year": 0, "expenses_last_year": 0},
        "bki": [],
    },
    "5001777021": {
        "egrul": {
            "kind": "OOO", "ogrn": "1185001177702", "name": "ООО «Долговой центр»",
            "registered_at": "2018-11-01", "status": "ACTIVE",
            "address": "Московская обл., г. Балашиха, ш. Энтузиастов, д. 3", "okved": "45.20",
            "okved_name": "Техническое обслуживание и ремонт автотранспорта",
            "director": "Гусев Артём Романович",
            "founders": [{"name": "Гусев А. Р.", "share": 100}],
        },
        "transparent": {"employees": 12, "tax_arrears": 0, "taxes_paid_last_year": 900_000,
                        "revenue_last_year": 18_000_000, "expenses_last_year": 16_900_000},
        "bki": [_loan("Банк Д", 1_600_000, 95_000, "06.2027", overdue_days=45, overdue_amount=380_000,
                      history=[{"days": 45, "amount": 380_000, "months_ago": 0}, {"days": 20, "amount": 95_000, "months_ago": 5}])],
    },
    "6022333043": {
        "egrul": {
            "kind": "OOO", "ogrn": "1206000233304", "name": "ООО «Сбой связи»",
            "registered_at": "2020-04-14", "status": "ACTIVE",
            "address": "г. Псков, ул. Связистов, д. 2", "okved": "61.10",
            "okved_name": "Деятельность в области связи на базе проводных технологий",
            "director": "Тихонов Глеб Андреевич",
            "founders": [{"name": "Тихонов Г. А.", "share": 100}],
        },
        "transparent": {"employees": 9, "tax_arrears": 0, "taxes_paid_last_year": 700_000,
                        "revenue_last_year": 12_000_000, "expenses_last_year": 10_800_000},
        "bki": [],
    },
    "4703444053": {
        "egrul": {
            "kind": "OOO", "ogrn": "1164700344405", "name": "ООО «Закат»",
            "registered_at": "2016-07-07", "status": "LIQUIDATING",
            "address": "Ленинградская обл., г. Всеволожск, ул. Закатная, д. 1", "okved": "47.19",
            "okved_name": "Торговля розничная прочая",
            "director": "Ликвидатор Сидоров П. П.",
            "founders": [{"name": "Сидоров П. П.", "share": 100}],
        },
        "transparent": {"employees": 0, "tax_arrears": 54_000, "taxes_paid_last_year": 100_000,
                        "revenue_last_year": 3_000_000, "expenses_last_year": 3_900_000},
        "bki": [],
    },
    # Ни к кому не привязана в демо: на ней показывают «Добавить компанию по ИНН» и путь с нуля
    "7713550120": {
        "egrul": {
            "kind": "OOO", "ogrn": "1207700355012", "name": "ООО «Новый горизонт»",
            "registered_at": "2020-08-18", "status": "ACTIVE",
            "address": "г. Москва, ул. Новая, д. 7", "okved": "47.91",
            "okved_name": "Торговля розничная по почте или по информационно-коммуникационной сети Интернет",
            "director": "Белова Наталья Викторовна",
            "founders": [{"name": "Белова Н. В.", "share": 100}],
        },
        "transparent": {"employees": 18, "tax_arrears": 0, "taxes_paid_last_year": 2_900_000,
                        "revenue_last_year": 52_000_000, "expenses_last_year": 46_800_000},
        "bki": [_loan("Банк Ж", 1_200_000, 70_000, "04.2027")],
    },
    "772012345670": {
        "egrul": {
            "kind": "IP", "ogrn": "321774600012345", "name": "ИП Козлова Елена Андреевна",
            "registered_at": "2021-05-20", "status": "ACTIVE",
            "address": "г. Москва", "okved": "96.02",
            "okved_name": "Предоставление услуг парикмахерскими и салонами красоты",
            "director": "Козлова Елена Андреевна",
            "founders": [],
        },
        "transparent": {"employees": 6, "tax_arrears": 0, "taxes_paid_last_year": 380_000,
                        "revenue_last_year": 9_500_000, "expenses_last_year": 7_900_000},
        "bki": [],
    },
}


def _generated(inn: str) -> dict:
    """Детерминированная синтетическая компания для любого другого корректного ИНН."""
    h = int(hashlib.sha256(inn.encode()).hexdigest(), 16)
    is_ip = len(inn) == 12
    years = 1 + h % 12
    revenue = (5 + h % 120) * 1_000_000
    names = ["Альфа-Сервис", "Горизонт", "Полюс", "Меридиан", "Вектор", "Профиль", "Ресурс", "Импульс"]
    name = names[h % len(names)]
    return {
        "egrul": {
            "kind": "IP" if is_ip else "OOO",
            "ogrn": str(10**14 + h % 10**14)[:15 if is_ip else 13],
            "name": f"ИП Иванов И. И. ({name})" if is_ip else f"ООО «{name}»",
            "registered_at": (date.today() - timedelta(days=365 * years + h % 300)).isoformat(),
            "status": "ACTIVE",
            "address": "г. Москва (синтетический адрес)",
            "okved": "46.90", "okved_name": "Торговля оптовая неспециализированная",
            "director": "Иванов Иван Иванович",
            "founders": [] if is_ip else [{"name": "Иванов И. И.", "share": 100}],
        },
        "transparent": {"employees": 3 + h % 80, "tax_arrears": 0 if h % 5 else 45_000,
                        "taxes_paid_last_year": revenue // 25,
                        "revenue_last_year": revenue, "expenses_last_year": int(revenue * 0.92)},
        "bki": [] if h % 3 == 0 else [_loan("Банк Е", revenue // 10, revenue // 150, "12.2027")],
    }


def get_company(inn: str) -> dict | None:
    if not inn_is_valid(inn):
        return None
    return COMPANIES.get(inn) or _generated(inn)
