"""Демо-данные: то, что жюри увидит сразу после docker compose up."""
from __future__ import annotations

from app.models import Application, Company, User
from app.seed import seed_applications
from app.services.analyst import process_metrics, queue_counts


def test_seed_statuses_and_realistic_timeline(db):
    seed_applications(db)
    got = {a.number[-4:]: a.status for a in db.query(Application).order_by(Application.id)}
    assert got == {"0001": "DISBURSED", "0002": "MANUAL_REVIEW", "0003": "NEEDS_INFO", "0004": "DRAFT",
                   "0005": "MANUAL_REVIEW", "0006": "MANUAL_REVIEW", "0007": "MANUAL_REVIEW", "0008": "APPROVED_AUTO"}

    m = process_metrics(db)
    assert m["stage_avg"]["DRAFT"] > 10 * 60  # заполнение заявки — минуты, а не 0 с
    assert m["stage_avg"]["SIGNING"] > 60 * 60  # подпись директора — часы

    maria = db.query(User).filter_by(email="maria@bank.local").one()
    counts = queue_counts(db, maria)
    assert counts["overdue"] == 1  # одна просроченная задача — видно подсветку SLA в очереди
    assert counts["mine"] == 1 and counts["unassigned"] == 3 and counts["waiting"] == 1

    seed_applications(db)  # идемпотентно
    assert db.query(Application).count() == 8


def test_every_demo_company_has_someone_who_can_sign(db):
    """Иначе одобренная заявка застрянет на подписании: подписать договор некому."""
    for c in db.query(Company).filter(Company.members.any()):
        assert any(m.can_sign for m in c.members), c.name
