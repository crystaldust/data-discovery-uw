"""REST API under /api/v1: four GET endpoints behind API keys, metered per minute.

Responses are {"data": ..., "meta": {...}}; errors are {"error": {"code", "message"}}.
Error messages name fields, never echo what was sent.
"""

import logging
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from ddm.access.keys import check_key, meter
from ddm.query.detail import EntityDetail, get_entity, parse_entity_id
from ddm.query.discoveries import (
    CLASSIFICATIONS,
    SOURCES,
    DiscoveryRow,
    list_recent_discoveries,
)
from ddm.query.filters import (
    MAX_OFFSET,
    MAX_PAGE_SIZE,
    QueryInputError,
    page_request,
    parse_filters,
)
from ddm.query.metrics import Metrics, get_metrics
from ddm.query.search import EntitySummary, search_entities

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"
DEFAULT_PAGE_SIZE = 20
EntityType = Literal["podcast", "journalist"]
# Built from the query layer's lists, so the three entry points cannot drift apart.
Classification = Literal[CLASSIFICATIONS]
DiscoverySource = Literal[SOURCES]
Limit = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]
Offset = Annotated[int, Query(ge=0, le=MAX_OFFSET)]

bearer = HTTPBearer(auto_error=False, description="API key: Authorization: Bearer ddm_...")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.headers = status, code, message, headers


class PageMeta(BaseModel):
    limit: int
    offset: int
    has_more: bool


class EmptyMeta(BaseModel):
    pass


class EntityList(BaseModel):
    data: list[EntitySummary]
    meta: PageMeta


class EntityResponse(BaseModel):
    data: EntityDetail
    meta: EmptyMeta


class DiscoveryList(BaseModel):
    data: list[DiscoveryRow]
    meta: PageMeta


class MetricsResponse(BaseModel):
    data: Metrics
    meta: EmptyMeta


def error_body(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def metered(name: str):
    """Dependency: a live key within its per-minute limit, counted under this route."""

    def dependency(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> int:
        key = credentials.credentials if credentials else request.headers.get("x-api-key")
        pool = request.app.state.pool
        with pool.connection() as conn:
            key_id = check_key(conn, key)
            if key_id is None:
                raise ApiError(
                    401,
                    "unauthorized",
                    "a valid API key is required",
                    {"WWW-Authenticate": "Bearer"},
                )
            result = meter(conn, key_id, "api", name)
        logger.info("step=api key_id=%d route=%s allowed=%s", key_id, name, result.allowed)
        if not result.allowed:
            raise ApiError(
                429,
                "rate_limited",
                "rate limit exceeded",
                {"Retry-After": str(result.retry_after_seconds)},
            )
        return key_id

    return dependency


router = APIRouter(prefix=API_PREFIX, tags=["read-only"])


def _query_error(exc: QueryInputError) -> ApiError:
    return ApiError(422, "invalid_request", str(exc))


@router.get("/entities", response_model=EntityList, dependencies=[Depends(metered("entities"))])
def list_entities(
    request: Request,
    q: Annotated[str | None, Query(description="name to search for (fuzzy)")] = None,
    entity_type: EntityType | None = None,
    language: Annotated[str | None, Query(description="language code, e.g. en")] = None,
    category: str | None = None,
    outlet: str | None = None,
    active_within_days: Annotated[int | None, Query(description="one of 7, 30, 90, 365")] = None,
    has_contact: bool | None = None,
    first_seen: Literal["historical", "discovered"] | None = None,
    limit: Limit = DEFAULT_PAGE_SIZE,
    offset: Offset = 0,
) -> dict:
    """Search podcasts and journalists; without q, most recently active first."""
    try:
        filters = parse_filters(
            q=q,
            entity_type=entity_type,
            language=language,
            category=category,
            outlet=outlet,
            active_within_days=active_within_days,
            has_contact=has_contact,
            first_seen=first_seen,
        )
    except QueryInputError as exc:
        raise _query_error(exc) from None
    with request.app.state.pool.connection() as conn:
        page = search_entities(conn, filters, page_request(limit, offset))
    meta = PageMeta(limit=page.limit, offset=page.offset, has_more=page.has_more)
    return {"data": list(page.items), "meta": meta}


@router.get(
    "/entities/{entity_id}",
    response_model=EntityResponse,
    dependencies=[Depends(metered("entity"))],
    responses={404: {"description": "no entity with that id"}},
)
def read_entity(request: Request, entity_id: str) -> dict:
    """One entity with its identifiers, source records, masked contact routes,
    latest observations, discoveries and pending candidate matches."""
    try:
        eid: UUID = parse_entity_id(entity_id)
    except QueryInputError:
        raise ApiError(404, "not_found", "no entity with that id") from None
    with request.app.state.pool.connection() as conn:
        detail = get_entity(conn, eid)
    if detail is None:
        raise ApiError(404, "not_found", "no entity with that id")
    return {"data": detail, "meta": {}}


@router.get(
    "/discoveries", response_model=DiscoveryList, dependencies=[Depends(metered("discoveries"))]
)
def list_discoveries(
    request: Request,
    classification: Classification | None = None,
    source: DiscoverySource | None = None,
    entity_type: EntityType | None = None,
    limit: Limit = DEFAULT_PAGE_SIZE,
    offset: Offset = 0,
) -> dict:
    """Records first seen by discovery runs, newest run first."""
    with request.app.state.pool.connection() as conn:
        page = list_recent_discoveries(
            conn,
            page_request(limit, offset),
            classification=classification,
            source=source,
            entity_type=entity_type,
        )
    meta = PageMeta(limit=page.limit, offset=page.offset, has_more=page.has_more)
    return {"data": list(page.items), "meta": meta}


@router.get("/metrics", response_model=MetricsResponse, dependencies=[Depends(metered("metrics"))])
def read_metrics(request: Request) -> dict:
    """Entity counts, freshness, contact route coverage (counts only), discoveries, runs."""
    with request.app.state.pool.connection() as conn:
        return {"data": get_metrics(conn), "meta": {}}


def api_error_response(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(error_body(exc.code, exc.message), exc.status, headers=exc.headers)


def validation_message(exc: RequestValidationError) -> str:
    """Which parameters were wrong, without the values that were sent."""
    names = sorted({str(e["loc"][-1]) for e in exc.errors() if e.get("loc")})
    return f"invalid parameter(s): {', '.join(names)}" if names else "invalid request"
