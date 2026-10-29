"""The web application: pages, the REST API, and the MCP endpoint at /mcp.

Everything reads through one read-only connection pool. The pool opens lazily, so a cold
start does not wait for the database; when the database is unreachable, requests get a
generic 503 that names no host.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from psycopg_pool import ConnectionPool, PoolTimeout
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from ddm.config import WebConfig
from ddm.web.api import API_PREFIX, ApiError, api_error_response, error_body, validation_message
from ddm.web.api import router as api_router
from ddm.web.auth import SESSION_MAX_AGE_SECONDS, LoginRequired, new_login_limiter
from ddm.web.db import make_pool
from ddm.web.headers import McpPathMiddleware, SecurityHeadersMiddleware, docs_csp
from ddm.web.mcp import build_mcp
from ddm.web.pages import router as pages_router
from ddm.web.pages import templates

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
SESSION_COOKIE = "ddm_session"
UNAVAILABLE = "the database is not available right now; try again shortly"
OPENAPI_URL = f"{API_PREFIX}/openapi.json"

# The pool logs failed connection attempts with the server's address; runtime logs should
# show that the database is down, not where it is.
logging.getLogger("psycopg.pool").setLevel(logging.ERROR)


def docs_page() -> HTMLResponse:
    return get_swagger_ui_html(
        openapi_url=OPENAPI_URL,
        title="Data Discovery Demo API",
        swagger_js_url="/static/swagger/swagger-ui-bundle.js",
        swagger_css_url="/static/swagger/swagger-ui.css",
        swagger_favicon_url="/static/swagger/favicon-32x32.png",
    )


def _is_api(request: Request) -> bool:
    return request.url.path.startswith(API_PREFIX)


def _login_required(request: Request, _: LoginRequired) -> Response:
    if request.headers.get("hx-request") == "true":
        return Response(status_code=401, headers={"HX-Redirect": "/login"})
    return RedirectResponse("/login", status_code=303)


def _unavailable(request: Request, exc: Exception) -> Response:
    logger.warning("step=web error=%s path=%s", type(exc).__name__, request.url.path)
    if _is_api(request):
        return JSONResponse(error_body("unavailable", UNAVAILABLE), 503)
    context = {"user": None, "title": "Unavailable", "message": UNAVAILABLE}
    return templates.TemplateResponse(request, "error.html", context, status_code=503)


TOO_SLOW = "the query took too long; narrow the search and try again"


def _too_slow(request: Request, exc: Exception) -> Response:
    """The statement timeout fired: a slow query, not an outage."""
    logger.warning("step=web error=%s path=%s", type(exc).__name__, request.url.path)
    if _is_api(request):
        return JSONResponse(error_body("query_timeout", TOO_SLOW), 503)
    context = {"user": None, "title": "Too slow", "message": TOO_SLOW}
    return templates.TemplateResponse(request, "error.html", context, status_code=503)


def _invalid(request: Request, exc: RequestValidationError) -> Response:
    if _is_api(request):
        return JSONResponse(error_body("invalid_request", validation_message(exc)), 422)
    context = {"user": None, "title": "Bad request", "message": validation_message(exc)}
    return templates.TemplateResponse(request, "error.html", context, status_code=422)


def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
    if _is_api(request):
        return JSONResponse(
            error_body("http_error", str(exc.detail)), exc.status_code, headers=exc.headers
        )
    return Response(str(exc.detail), exc.status_code, headers=exc.headers)


def _database_error(request: Request, exc: psycopg.Error) -> Response:
    """Anything else from the driver: logged by class, answered without its text."""
    logger.error("step=web error=%s path=%s", type(exc).__name__, request.url.path)
    if _is_api(request):
        return JSONResponse(error_body("internal", "the request could not be completed"), 500)
    context = {"user": None, "title": "Error", "message": "The request could not be completed."}
    return templates.TemplateResponse(request, "error.html", context, status_code=500)


def _add_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(LoginRequired, _login_required)
    app.add_exception_handler(ApiError, api_error_response)
    app.add_exception_handler(RequestValidationError, _invalid)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(PoolTimeout, _unavailable)
    app.add_exception_handler(psycopg.errors.QueryCanceled, _too_slow)
    app.add_exception_handler(psycopg.OperationalError, _unavailable)
    app.add_exception_handler(psycopg.Error, _database_error)


def create_app(config: WebConfig, *, pool: ConnectionPool | None = None) -> FastAPI:
    """A pool passed in (tests) is opened and closed by its owner, not by the app."""
    own_pool = pool is None
    pool = pool or make_pool(config.readonly_db_url)
    mcp_app = build_mcp(pool).http_app(path="/", stateless_http=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if own_pool:
            pool.open()
        try:
            async with mcp_app.lifespan(app):
                yield
        finally:
            if own_pool:
                pool.close()

    app = FastAPI(
        title="Data Discovery Demo API",
        version="1",
        description="Read-only search over podcasts and journalists. Emails are masked.",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=OPENAPI_URL,
    )
    app.state.pool, app.state.config = pool, config
    app.state.login_failures = new_login_limiter()
    _add_error_handlers(app)
    _add_middleware(app, config)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(api_router)
    app.include_router(pages_router)
    app.mount("/mcp", mcp_app)
    return app


def _add_middleware(app: FastAPI, config: WebConfig) -> None:
    """Also adds /docs and /healthz, whose headers the middleware depends on."""
    docs = docs_page()
    app.add_middleware(SecurityHeadersMiddleware, docs_policy=docs_csp(docs.body.decode()))
    app.add_middleware(
        SessionMiddleware,
        secret_key=config.session_secret,
        session_cookie=SESSION_COOKIE,
        max_age=SESSION_MAX_AGE_SECONDS,
        same_site="lax",
        https_only=config.secure_cookies,
    )
    app.add_middleware(McpPathMiddleware)

    @app.get("/docs", include_in_schema=False)
    def swagger_ui() -> HTMLResponse:
        return docs

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}
