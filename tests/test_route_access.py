"""Каждый маршрут, кроме публичных, без входа закрыт: страницы ведут на вход, API отвечает 401, формы — 403 (CSRF)."""
from __future__ import annotations

import re

from fastapi.testclient import TestClient

PUBLIC = {"/", "/login", "/register", "/logout", "/health", "/favicon.ico", "/api/health", "/api/docs",
          "/api/openapi.json", "/docs/oauth2-redirect", "/api/auth/login", "/api/auth/register",
          "/api/auth/logout",
          "/api/products"}  # каталог выдаваемых продуктов — публичная информация, как тарифы на сайте банка


def _routes(routes):
    """Плоский список маршрутов: FastAPI хранит подключённые роутеры вложенными объектами."""
    for r in routes:
        if hasattr(r, "effective_candidates"):
            yield from _routes(r.effective_candidates())
        else:
            yield r


def test_every_non_public_route_requires_login():
    from app.main import app

    c = TestClient(app, follow_redirects=False)
    checked, opened = 0, []
    for route in _routes(app.routes):
        path, methods = getattr(route, "path", ""), getattr(route, "methods", None) or set()
        if not methods or path in PUBLIC or path.startswith("/static"):
            continue
        url = re.sub(r"\{[^}]+\}", "1", path)
        for method in sorted(methods - {"HEAD", "OPTIONS"}):
            r = c.request(method, url, json={} if url.startswith("/api/") and method != "GET" else None)
            checked += 1
            ok = r.status_code in (401, 403) or (r.status_code == 303 and "/login" in r.headers.get("location", ""))
            if not ok:
                opened.append(f"{method} {path} -> {r.status_code}")
    assert checked > 60 and opened == []
