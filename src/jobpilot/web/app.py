"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from jobpilot import __version__
from jobpilot.config import LOCAL_HOSTS, Settings, get_settings
from jobpilot.db import make_engine, make_sessionmaker, migrate
from jobpilot.logging_setup import configure_logging

log = logging.getLogger(__name__)

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class SameOriginMiddleware(BaseHTTPMiddleware):
    """Reject cross-site state-changing requests (CSRF protection).

    Browsers attach ``Origin`` (or at least ``Referer``/``Sec-Fetch-Site``) to
    cross-site form posts, so a malicious page cannot drive this local app.
    """

    def __init__(self, app, allowed_hosts: set[str]):
        super().__init__(app)
        self.allowed_hosts = allowed_hosts

    async def dispatch(self, request: Request, call_next):
        if request.method in UNSAFE_METHODS:
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin and origin != "null":
                host = urlsplit(origin).hostname
                if host not in self.allowed_hosts:
                    log.warning("blocked cross-origin request", extra={"origin": origin, "path": request.url.path})
                    return PlainTextResponse("Cross-origin request blocked", status_code=403)
            elif origin == "null" or request.headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
                log.warning("blocked cross-origin request", extra={
                    "origin": origin, "sec_fetch_site": request.headers.get("sec-fetch-site"), "path": request.url.path})
                return PlainTextResponse("Cross-origin request blocked", status_code=403)
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        # "same-origin": no referrer is ever sent to other sites, but the browser keeps sending our own
        # Origin on form posts. ("no-referrer" makes Chrome send "Origin: null", which the CSRF check rejects.)
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response


ERROR_TITLES = {400: "Invalid request", 403: "Forbidden", 404: "Not found", 405: "Method not allowed",
                413: "Request too large", 500: "Something went wrong"}


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith(("/health", "/api/", "/openapi")) or \
        "application/json" in request.headers.get("accept", "") and "text/html" not in request.headers.get("accept", "")


def _error_response(request: Request, status: int, detail: str):
    if _wants_json(request):
        return JSONResponse({"detail": detail}, status_code=status)
    from jobpilot.web.deps import templates

    return templates.TemplateResponse(
        request, "error.html",
        {"status": status, "title": ERROR_TITLES.get(status, "Error"), "detail": detail,
         "settings": request.app.state.settings, "msg": None, "err": None},
        status_code=status)


def register_error_handlers(app: FastAPI) -> None:
    """Controlled, informative errors instead of raw tracebacks or FastAPI's JSON 422s."""

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else ERROR_TITLES.get(exc.status_code, "Error")
        if exc.status_code == 404 and detail == "Not Found":
            detail = f"No page at {request.url.path}"
        return _error_response(request, exc.status_code, detail)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        errs = exc.errors()
        parts = []
        for e in errs[:3]:
            loc = ".".join(str(x) for x in e.get("loc", ()) if x not in ("body", "query", "path"))
            parts.append(f"{loc}: {e.get('msg')}" if loc else str(e.get("msg")))
        return _error_response(request, 400, "Invalid input — " + "; ".join(parts))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):  # last-resort safety net; always logged
        log.exception("unhandled error", extra={"path": request.url.path, "method": request.method})
        return _error_response(request, 500, "An unexpected error occurred. Details were written to the log "
                                             "(System → Logs). No changes from this request were saved.")


def seed_reference_data(engine) -> None:
    """Built-in rows the UI relies on (email templates). Done at start-up so page views never write."""
    from jobpilot.db import make_sessionmaker
    from jobpilot.services.drafts import ensure_builtin_templates

    with make_sessionmaker(engine)() as s:
        ensure_builtin_templates(s)
        s.commit()


def _recover_interrupted_tasks(settings: Settings, engine) -> None:
    """Tasks left "running" by a crashed process are recorded as failed (visible on System and /health)."""
    from datetime import timedelta

    from jobpilot.db import make_sessionmaker
    from jobpilot.services.tasks import mark_interrupted

    with make_sessionmaker(engine)() as s:
        if mark_interrupted(s, settings, min_age=timedelta(minutes=1)):
            s.commit()


def create_app(settings: Settings | None = None, run_migrations: bool = True) -> FastAPI:
    settings = settings or get_settings()
    settings.ensure_dirs()
    configure_logging(settings)
    engine = make_engine(settings)
    if run_migrations:
        migrate(settings, engine)
    settings.secure_files()
    seed_reference_data(engine)
    _recover_interrupted_tasks(settings, engine)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        # Safe shutdown: stop the optional scheduler thread (waits for a running task to finish).
        thread = getattr(_app.state, "scheduler_thread", None)
        if thread is not None:
            thread.stop_event.set()
            thread.join(timeout=60)
        _app.state.engine.dispose()

    # No Swagger/ReDoc UI (they load third-party CDN assets); the JSON schema is opt-in.
    app = FastAPI(title="JobPilot", version=__version__, docs_url=None, redoc_url=None,
                  openapi_url="/openapi.json" if settings.enable_openapi else None, lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)

    allowed = set(LOCAL_HOSTS) | {"testserver"}
    if settings.host not in LOCAL_HOSTS:
        allowed.add(settings.host)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(SameOriginMiddleware, allowed_hosts=allowed)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=sorted(allowed))

    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

    from jobpilot.web.routes import register_routes

    register_routes(app)
    register_error_handlers(app)

    return app
