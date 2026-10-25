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
