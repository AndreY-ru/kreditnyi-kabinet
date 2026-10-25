"""Тестовое окружение: БД (SQLite в памяти или TEST_DATABASE_URL), моки ФНС/БКИ внутри процесса."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", os.getenv("TEST_DATABASE_URL", "sqlite://"))

from app import db as dbm  # noqa: E402
from app import security  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import Base  # noqa: E402
from app.services import integrations  # noqa: E402
from mocks.bki import app as bki_app  # noqa: E402
from mocks.fns import app as fns_app  # noqa: E402

security.ITERATIONS = 1000  # быстрые хеши в тестах
settings.pipeline_step_delay = 0
settings.enrich_retry_delay = 0


@pytest.fixture(autouse=True)
def _env(tmp_path):
    settings.upload_dir = str(tmp_path / "uploads")
    settings.database_url = os.environ["DATABASE_URL"]
    engine = dbm.init_engine(settings.database_url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    from app import ratelimit

    ratelimit.clear_all()  # счётчики попыток входа — свои у каждого теста
    integrations.http_factory["FNS_MOCK"] = lambda base: TestClient(fns_app)
    integrations.http_factory["BKI_MOCK"] = lambda base: TestClient(bki_app)
    from app.seed import seed_reference

    with dbm.SessionLocal() as db:
        seed_reference(db)
    yield
    integrations.http_factory.clear()
    engine.dispose()


@pytest.fixture
def db():
    with dbm.SessionLocal() as s:
        yield s


def api_client(email: str) -> TestClient:
    from app.main import app

    c = TestClient(app)
    r = c.post("/api/auth/login", json={"email": email, "password": "demo"})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture
def anna():
    return api_client("anna@demo.local")


@pytest.fixture
def igor():
    return api_client("igor@demo.local")


@pytest.fixture
def maria():
    return api_client("maria@bank.local")


@pytest.fixture
def petr():
    return api_client("petr@bank.local")


@pytest.fixture
def elena():
    return api_client("elena@demo.local")
