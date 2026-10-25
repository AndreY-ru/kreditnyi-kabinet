"""Безопасность: подпись сессий, заголовки, ограничение попыток входа."""
from __future__ import annotations

import base64
import json

from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from app.config import KNOWN_DEMO_SECRETS, resolve_secret


def test_secret_from_repo_is_never_used():
    """Секрет из репозитория публичен: подписанную им cookie мог бы сделать кто угодно."""
    for value in (None, "", *KNOWN_DEMO_SECRETS):
        secret = resolve_secret(value)
        assert secret not in KNOWN_DEMO_SECRETS and len(secret) >= 32
    assert resolve_secret("my-own-long-secret-from-env-file-123") == "my-own-long-secret-from-env-file-123"


def test_forged_session_cookie_with_public_secret_is_rejected():
    from app.main import app

    c = TestClient(app)
    for public_secret in KNOWN_DEMO_SECRETS:
        cookie = TimestampSigner(public_secret).sign(base64.b64encode(json.dumps({"uid": 1}).encode()))
        r = c.get("/api/auth/me", cookies={"kabinet_session": cookie.decode()})
        assert r.status_code == 401, public_secret


def test_security_headers_and_strict_csp():
    from app.main import app

    c = TestClient(app)
    r = c.get("/login")
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    csp = r.headers["content-security-policy"]
    assert "script-src 'self';" in csp and "frame-ancestors 'none'" in csp
    assert "content-security-policy" not in c.get("/api/docs").headers  # Swagger работает


def test_templates_have_no_inline_scripts():
    """Строгий CSP (script-src 'self') блокирует onclick/onsubmit — они должны жить в app.js."""
    import re
    from pathlib import Path

    for p in Path("app/templates").rglob("*.html"):
        text = p.read_text(encoding="utf-8")
        assert not re.search(r"\son[a-z]+=\"|<script>|javascript:", text), p


def test_password_guessing_is_limited():
    from app.main import app

    c = TestClient(app)
    for _ in range(5):
        assert c.post("/api/auth/login", json={"email": "anna@demo.local", "password": "wrong"}).status_code == 401
    r = c.post("/api/auth/login", json={"email": "anna@demo.local", "password": "demo"})  # даже верный пароль
    assert r.status_code == 429 and "Retry-After" in r.headers and "через" in r.json()["message"]
    assert c.post("/api/auth/login", json={"email": "igor@demo.local", "password": "demo"}).status_code == 200


def test_web_login_is_limited_too():
    import re

    from app.main import app

    c = TestClient(app)
    tok = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/login").text).group(1)
    for _ in range(5):
        c.post("/login", data={"email": "maria@bank.local", "password": "x", "csrf_token": tok, "next": ""})
    r = c.post("/login", data={"email": "maria@bank.local", "password": "demo", "csrf_token": tok, "next": ""})
    assert r.status_code == 429 and "Слишком много попыток" in r.text


def test_inactive_user_cannot_log_in_via_api(db):
    from app.main import app
    from app.models import User

    u = db.query(User).filter_by(email="elena@demo.local").one()
    u.is_active = False
    db.commit()
    assert TestClient(app).post("/api/auth/login", json={"email": "elena@demo.local", "password": "demo"}).status_code == 401


def test_oversized_request_is_rejected_before_parsing(monkeypatch):
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "max_upload_mb", 0)  # лимит запроса — 1 МБ
    r = TestClient(app).post("/api/applications/1/info-response", files={"files": ("big.pdf", b"x" * 1_200_000)})
    assert r.status_code == 413 and r.json()["error"] == "too_large"


def test_file_over_limit_is_not_read_whole(anna, monkeypatch):
    from app.config import settings
    from tests.helpers import SV, draft

    monkeypatch.setattr(settings, "max_upload_mb", 1)
    app = draft(anna, SV, "WORKING", 1_000_000, 12)
    r = anna.post(f"/api/applications/{app['id']}/info-response", files={"files": ("big.pdf", b"x" * 1_100_000)})
    assert r.status_code == 422 and r.json()["error"] == "file_too_large"


def test_api_rejects_form_encoded_json_and_cookie_is_httponly_lax(anna):
    """Чужой сайт может отправить только форму (text/plain, urlencoded) — JSON API её не примет."""
    from app.main import app

    r = anna.post("/api/companies", content='{"inn": "7713550120", "position": "x"}',
                  headers={"Content-Type": "text/plain"})
    assert r.status_code == 422
    cookie = TestClient(app).post("/api/auth/login", json={"email": "igor@demo.local", "password": "demo"})
    assert "httponly" in cookie.headers["set-cookie"].lower() and "samesite=lax" in cookie.headers["set-cookie"].lower()
