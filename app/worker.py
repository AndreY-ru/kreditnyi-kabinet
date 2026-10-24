"""Воркер конвейера: забирает задачи из outbox (SELECT … FOR UPDATE SKIP LOCKED) и следит за сроками.

Запуск: python -m app.worker
"""
from __future__ import annotations

import logging
import signal
import threading
import time
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal, get_engine
from app.domain.states import OFFER_STATES, S
from app.domain.transitions import ActorRef, DomainError, transition
from app.models import Application, InfoRequest, Job, Offer, ReviewTask, utcnow
from app.services.pipeline import process_application

log = logging.getLogger("worker")
MAX_JOB_ATTEMPTS = 3


def claim_job(db: Session) -> Job | None:
    stmt = (select(Job).where(Job.status == "PENDING", Job.run_after <= utcnow())
            .order_by(Job.id).limit(1).with_for_update(skip_locked=True))
    job = db.scalar(stmt)
    if job is None:
        db.rollback()
        return None
    job.status = "RUNNING"
    job.attempts += 1
    job.updated_at = utcnow()
    db.commit()
    return job


def run_job(db: Session, job: Job) -> None:
    try:
        if job.kind == "PROCESS_APPLICATION":
            process_application(db, job.application_id)
        job = db.get(Job, job.id)  # type: ignore[assignment]
        job.status = "DONE"
        job.updated_at = utcnow()
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        log.exception("Задача %s упала", job.id)
        job = db.get(Job, job.id)  # type: ignore[assignment]
        job.last_error = str(exc)[:2000]
        job.status = "PENDING" if job.attempts < MAX_JOB_ATTEMPTS else "FAILED"
        job.run_after = utcnow() + timedelta(seconds=5 * job.attempts)
        job.updated_at = utcnow()
        db.commit()


def run_pending(db: Session, limit: int = 100) -> int:
    """Обработать все готовые задачи (используется в тестах и сидах)."""
    n = 0
    while n < limit:
        job = claim_job(db)
        if job is None:
            break
        run_job(db, job)
        n += 1
    return n


def expire_stale(db: Session) -> int:
    """Истечение предложений и запросов данных (переходы в EXPIRED)."""
    now = utcnow()
    n = 0
    system = ActorRef.system()
    for offer in db.scalars(select(Offer).where(Offer.status == "ACTIVE", Offer.valid_until < now)).all():
        app = db.get(Application, offer.application_id)
        offer.status = "EXPIRED"
        if app and app.status in OFFER_STATES:
            try:
                transition(db, app, S.EXPIRED, event="Срок предложения истёк", actor=system)
                n += 1
            except DomainError:
                db.rollback()
                continue
    for req in db.scalars(select(InfoRequest).where(InfoRequest.status == "OPEN", InfoRequest.due_at < now)).all():
        app = db.get(Application, req.application_id)
        req.status = "EXPIRED"
        if app and app.status == S.NEEDS_INFO:
            try:
                transition(db, app, S.EXPIRED, event="Срок ответа истёк", actor=system)
                for t in db.scalars(select(ReviewTask).where(ReviewTask.application_id == app.id,
                                                             ReviewTask.closed_at.is_(None))):
                    t.closed_at = now
                n += 1
            except DomainError:
                db.rollback()
                continue
    db.commit()
    return n


_stop = threading.Event()


def loop() -> None:
    get_engine()
    last_expire = 0.0
    log.info("Воркер запущен")
    while not _stop.is_set():
        db = SessionLocal()
        try:
            did = run_pending(db, limit=10)
            if time.monotonic() - last_expire > 30:
                expire_stale(db)
                last_expire = time.monotonic()
        except Exception:  # noqa: BLE001
            log.exception("Ошибка цикла воркера")
            db.rollback()
            did = 0
        finally:
            db.close()
        if not did:
            _stop.wait(settings.worker_poll_interval)


def start_background() -> threading.Thread:
    t = threading.Thread(target=loop, name="pipeline-worker", daemon=True)
    t.start()
    return t


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    signal.signal(signal.SIGTERM, lambda *_: _stop.set())
    signal.signal(signal.SIGINT, lambda *_: _stop.set())
    loop()


if __name__ == "__main__":
    main()
