"""Точка входа FastAPI: JSON API (/api, OpenAPI на /api/docs) + HTML-кабинеты (Jinja2 + HTMX)."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app.api.routes import router as api_router
from app.config import settings
from app.db import SessionLocal, get_engine
from app.domain.transitions import DomainError
from app.security import LoginRequired
from app.templating import templates
from app.web.bank import router as bank_router
from app.web.client import router as client_router
from app.web.common import router as common_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_engine()
    if settings.run_worker_in_app:
        from app.worker import start_background

        start_background()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Умный кредитный кабинет предпринимателя",
        version="0.1.0",
        description="MVP для хакатона «Альфа Будущее». Все данные синтетические, ФНС и БКИ смоделированы.",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, session_cookie="kabinet_session",
                       same_site="lax", https_only=settings.secure_cookies, max_age=60 * 60 * 12)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    app.include_router(api_router)
    app.include_router(common_router)
    app.include_router(client_router)
    app.include_router(bank_router)

    @app.exception_handler(LoginRequired)
    async def _login(request: Request, exc: LoginRequired):
        if request.headers.get("HX-Request"):
            return JSONResponse({"error": "login"}, status_code=401, headers={"HX-Redirect": "/login"})
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    @app.exception_handler(DomainError)
    async def _domain(request: Request, exc: DomainError):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": exc.code, "message": exc.message, "details": exc.details},
                                status_code=exc.status_code)
        return templates.TemplateResponse(request, "error.html", {"code": exc.status_code, "message": exc.message},
                                          status_code=exc.status_code)

    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/") or request.headers.get("HX-Request"):
            return JSONResponse({"error": "http_error", "message": exc.detail}, status_code=exc.status_code)
        return templates.TemplateResponse(request, "error.html", {"code": exc.status_code, "message": exc.detail},
                                          status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        return JSONResponse({"error": "validation_failed", "message": "Проверьте данные запроса",
                             "details": jsonable_encoder(exc.errors())}, status_code=422)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():  # браузеры просят /favicon.ico даже при <link rel="icon">
        return FileResponse(Path(__file__).parent / "static" / "favicon.svg", media_type="image/svg+xml")

    @app.get("/health", include_in_schema=False)
    def health():
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        return {"status": "ok"}

    return app


app = create_app()
