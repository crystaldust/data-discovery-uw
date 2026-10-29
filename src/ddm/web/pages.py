"""The logged-in web pages: search (with HTMX partial results), entity detail,
discoveries, metrics and CSV export. Templates auto-escape; nothing is marked safe."""

import threading
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from psycopg_pool import ConnectionPool
from starlette.background import BackgroundTask

from ddm.query.detail import get_entity, parse_entity_id
from ddm.query.discoveries import CLASSIFICATIONS, SOURCES, list_recent_discoveries
from ddm.query.filters import (
    ACTIVE_WINDOWS_DAYS,
    MAX_OFFSET,
    MAX_QUERY_CHARS,
    QueryInputError,
    SearchFilters,
    page_request,
    parse_filters,
)
from ddm.query.metrics import get_metrics
from ddm.query.search import iter_export_csv, search_entities
from ddm.web.auth import (
    client_address,
    credentials_match,
    log_in,
    log_out,
    require_user,
)

PAGE_SIZE = 25
MAX_NUMBER_DIGITS = 6
# What a checked "has a contact route" box sends; anything else means "don't filter".
CHECKED = ("1", "on", "true")
# Each export holds a pooled connection while it streams; keep some for everyone else.
MAX_CONCURRENT_EXPORTS = 2
EXPORT_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_EXPORTS)
MAX_DESCRIPTION_CHARS = 400
EVAL_REPORT_URL = "https://github.com/crystaldust/data-discovery-uw/blob/main/eval/report.md"
SEARCH_FIELDS = (
    "q",
    "entity_type",
    "language",
    "category",
    "outlet",
    "active_within_days",
    "has_contact",
    "first_seen",
)
DISCOVERY_FIELDS = ("classification", "source", "entity_type")

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def day(value: datetime | None) -> str:
    return value.date().isoformat() if value else "—"


def web_url(value: Any) -> str | None:
    """Only absolute http(s) links are rendered as links (no javascript: and friends)."""
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    return value if parts.scheme in ("http", "https") and parts.netloc else None


def attr_text(value: Any) -> str:
    """A record attribute as one line of plain text (the template escapes it)."""
    if isinstance(value, Mapping):
        return ", ".join(f"{k}: {attr_text(v)}" for k, v in value.items())
    if isinstance(value, list | tuple):
        return "; ".join(attr_text(v) for v in value)
    text = "" if value is None else str(value)
    return text if len(text) <= MAX_DESCRIPTION_CHARS else text[:MAX_DESCRIPTION_CHARS] + "…"


templates.env.filters.update(day=day, web_url=web_url, attr_text=attr_text)

router = APIRouter(include_in_schema=False)
User = Annotated[str, Depends(require_user)]


def _small_number(raw: str) -> bool:
    """Digits only and short enough that int() is cheap and cannot raise."""
    return raw.isascii() and raw.isdigit() and len(raw) <= MAX_NUMBER_DIGITS


def _page_number(params: Mapping[str, str]) -> int:
    raw = params.get("page") or "1"
    if not _small_number(raw) or int(raw) < 1:
        raise QueryInputError("page must be a positive whole number")
    if (int(raw) - 1) * PAGE_SIZE > MAX_OFFSET:
        raise QueryInputError("page is too far; narrow the search instead")
    return int(raw)


def _days(raw: str | None) -> int | None:
    if not raw:
        return None
    if not _small_number(raw):
        raise QueryInputError("active_within_days must be a number of days")
    return int(raw)


def search_filters(params: Mapping[str, str]) -> SearchFilters:
    return parse_filters(
        q=params.get("q"),
        entity_type=params.get("entity_type"),
        language=params.get("language"),
        category=params.get("category"),
        outlet=params.get("outlet"),
        active_within_days=_days(params.get("active_within_days")),
        has_contact=True if params.get("has_contact") in CHECKED else None,
        first_seen=params.get("first_seen"),
    )


def _kept(params: Mapping[str, str], fields: tuple[str, ...]) -> dict[str, str]:
    return {k: params[k] for k in fields if params.get(k)}


def _page_links(kept: dict[str, str], page: int, has_more: bool) -> dict[str, str | None]:
    def link(number: int) -> str:
        return urlencode({**kept, "page": number}) if number > 1 else urlencode(kept)

    return {
        "prev": link(page - 1) if page > 1 else None,
        "next": link(page + 1) if has_more else None,
    }


def _search_context(request: Request) -> dict[str, Any]:
    params = request.query_params
    kept = _kept(params, SEARCH_FIELDS)
    context: dict[str, Any] = {
        "form": kept,
        "windows": ACTIVE_WINDOWS_DAYS,
        "max_chars": MAX_QUERY_CHARS,
        "export_query": urlencode(kept),
        "page": None,
        "error": None,
        "links": {},
    }
    try:
        filters = search_filters(params)
        number = _page_number(params)
    except QueryInputError as exc:
        return {**context, "error": str(exc)}
    with request.app.state.pool.connection() as conn:
        page = search_entities(conn, filters, page_request(PAGE_SIZE, (number - 1) * PAGE_SIZE))
    return {
        **context,
        "page": page,
        "number": number,
        "links": _page_links(kept, number, page.has_more),
    }


@router.get("/login")
def login_form(request: Request) -> Response:
    return templates.TemplateResponse(request, "login.html", {"error": None, "user": None})


@router.post("/login")
def login(
    request: Request,
    username: Annotated[str, Form(max_length=MAX_QUERY_CHARS)] = "",
    password: Annotated[str, Form(max_length=MAX_QUERY_CHARS)] = "",
) -> Response:
    client = client_address(request)
    if request.app.state.login_failures.blocked(client):
        context = {"error": "Too many failed attempts. Try again later.", "user": None}
        return templates.TemplateResponse(request, "login.html", context, status_code=429)
    if credentials_match(request, username, password):
        log_in(request)
        return RedirectResponse("/", status_code=303)
    request.app.state.login_failures.record(client)
    context = {"error": "Wrong username or password.", "user": None}
    return templates.TemplateResponse(request, "login.html", context, status_code=401)


@router.post("/logout")
def logout(request: Request) -> Response:
    log_out(request)
    return RedirectResponse("/login", status_code=303)


@router.get("/")
def search_page(request: Request, user: User) -> Response:
    context = {**_search_context(request), "user": user}
    status = 400 if context["error"] else 200
    return templates.TemplateResponse(request, "search.html", context, status_code=status)


@router.get("/search")
def search_results(request: Request, user: User) -> Response:
    """HTMX swaps just the results; a plain request (e.g. a reload) gets the whole page."""
    if request.headers.get("hx-request") != "true":
        return search_page(request, user)
    # Always 200: htmx does not swap 4xx responses, and the fragment carries the error.
    context = {**_search_context(request), "user": user}
    return templates.TemplateResponse(request, "_results.html", context)


@router.get("/entities/{entity_id}")
def entity_page(request: Request, entity_id: str, user: User) -> Response:
    try:
        eid = parse_entity_id(entity_id)
    except QueryInputError:
        return not_found(request, user)
    with request.app.state.pool.connection() as conn:
        detail = get_entity(conn, eid)
    if detail is None:
        return not_found(request, user)
    return templates.TemplateResponse(request, "entity.html", {"d": detail, "user": user})


def not_found(request: Request, user: str | None) -> Response:
    context = {"user": user, "message": "No entity with that id."}
    return templates.TemplateResponse(request, "error.html", context, status_code=404)


@router.get("/discoveries")
def discoveries_page(request: Request, user: User) -> Response:
    params = request.query_params
    kept = _kept(params, DISCOVERY_FIELDS)
    context: dict[str, Any] = {
        "user": user,
        "form": kept,
        "classifications": CLASSIFICATIONS,
        "sources": SOURCES,
        "error": None,
        "page": None,
        "links": {},
    }
    try:
        number = _page_number(params)
        with request.app.state.pool.connection() as conn:
            page = list_recent_discoveries(
                conn, page_request(PAGE_SIZE, (number - 1) * PAGE_SIZE), **kept
            )
    except QueryInputError as exc:
        context["error"] = str(exc)
        return templates.TemplateResponse(request, "discoveries.html", context, status_code=400)
    context.update(page=page, links=_page_links(kept, number, page.has_more))
    return templates.TemplateResponse(request, "discoveries.html", context)


@router.get("/metrics")
def metrics_page(request: Request, user: User) -> Response:
    with request.app.state.pool.connection() as conn:
        metrics = get_metrics(conn)
    context = {"user": user, "m": metrics, "eval_report_url": EVAL_REPORT_URL}
    return templates.TemplateResponse(request, "metrics.html", context)


@router.get("/export.csv")
def export_csv(request: Request, user: User) -> Response:
    """Every row matching the current filters, streamed (no row limit)."""
    try:
        filters = search_filters(request.query_params)
    except QueryInputError as exc:
        return Response(f"error: {exc}\n", status_code=400, media_type="text/plain")
    if not EXPORT_SLOTS.acquire(blocking=False):
        return Response(
            "error: too many exports running; try again in a minute\n",
            status_code=429,
            media_type="text/plain",
            headers={"Retry-After": "60"},
        )
    try:
        # Taken before the response starts, so an unavailable database is a clean 503.
        lease = ExportLease(request.app.state.pool)
    except BaseException:
        EXPORT_SLOTS.release()
        raise

    def chunks() -> Iterator[str]:
        try:
            yield from iter_export_csv(lease.conn, filters)
        finally:
            lease.finish()

    name = f"entities-{datetime.now(UTC):%Y%m%d}.csv"
    return StreamingResponse(
        chunks(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
        # Also runs when the client went away before the stream started.
        background=BackgroundTask(lease.abandon),
    )


class ExportLease:
    """One export's pooled connection and slot, given back exactly once.

    finish(): the stream ended normally; the connection goes back to the pool.
    abandon(): runs after the response, also when the client left mid-download. Then the
    suspended generator may still hold a transaction and cursor on the connection, and
    could touch it later when garbage-collected, so the connection is closed (the pool
    drops closed connections) rather than handed to another request.
    """

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool
        self.conn = pool.getconn()
        self._lock = threading.Lock()
        self._done = False

    def _give_back(self, close_first: bool) -> None:
        with self._lock:
            if self._done:
                return
            self._done = True
        try:
            if close_first:
                self.conn.close()
            self._pool.putconn(self.conn)
        finally:
            EXPORT_SLOTS.release()

    def finish(self) -> None:
        self._give_back(close_first=False)

    def abandon(self) -> None:
        self._give_back(close_first=True)
