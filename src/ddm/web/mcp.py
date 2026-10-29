"""The read-only MCP server: four query tools and ping, behind database API keys.

Every tool goes through the shared query layer and the read-only pool. Each call is
metered against the key's per-minute limit (shared with the REST API). Unexpected errors
are masked: clients see a generic message, never driver text or hostnames.
"""

import logging
from typing import Annotated, Any, Literal

import anyio
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext
from psycopg_pool import ConnectionPool
from pydantic import Field

from ddm.access.keys import check_key, meter
from ddm.query.detail import get_entity, parse_entity_id
from ddm.query.discoveries import CLASSIFICATIONS, SOURCES, list_recent_discoveries
from ddm.query.filters import MCP_MAX_RESULTS, QueryInputError, page_request, parse_filters
from ddm.query.jsonable import to_jsonable
from ddm.query.metrics import get_metrics
from ddm.query.search import search_entities

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Read-only search over a demo catalogue of podcasts and journalists. Every result says "
    "where its data came from: 'observed' means fetched and checked by this system on the "
    "date given; 'historical' means it comes from a catalogue import and was not re-checked. "
    "Contact emails are always masked. At most 50 results per call."
)

TOOL_NAMES = frozenset(
    {"ping", "search_entities", "get_entity", "list_recent_discoveries", "get_metrics"}
)
UNKNOWN_TOOL = "other"
Limit = Annotated[int, Field(ge=1, le=MCP_MAX_RESULTS, description="results to return (1-50)")]
EntityType = Literal["podcast", "journalist"]
Classification = Literal[CLASSIFICATIONS]
DiscoverySource = Literal[SOURCES]


class ApiKeyVerifier(TokenVerifier):
    """Bearer tokens are API keys checked against their stored hashes."""

    def __init__(self, pool: ConnectionPool) -> None:
        super().__init__()
        self._pool = pool

    def _check(self, token: str) -> int | None:
        with self._pool.connection() as conn:
            return check_key(conn, token)

    async def verify_token(self, token: str) -> AccessToken | None:
        # A database failure propagates (a 500), so clients retry rather than conclude
        # that their key is invalid, which is what returning None would tell them.
        key_id = await anyio.to_thread.run_sync(self._check, token)
        if key_id is None:
            return None
        # The key itself is not kept on the token object.
        return AccessToken(token="redacted", client_id=str(key_id), scopes=[])  # noqa: S106


class MeterMiddleware(Middleware):
    """Counts each tool call against the caller's key; refuses calls over the limit."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def _meter(self, key_id: int, tool: str):
        with self._pool.connection() as conn:
            return meter(conn, key_id, "mcp", tool)

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        token = get_access_token()
        if token is not None:
            # The name is whatever the client sent: count unknown ones under one bucket.
            name = context.message.name
            tool = name if name in TOOL_NAMES else UNKNOWN_TOOL
            result = await anyio.to_thread.run_sync(self._meter, int(token.client_id), tool)
            logger.info(
                "step=mcp key_id=%s tool=%s allowed=%s", token.client_id, tool, result.allowed
            )
            if not result.allowed:
                raise ToolError(
                    f"rate limit exceeded; retry in {result.retry_after_seconds} seconds"
                )
        return await call_next(context)


# Python names end in _tool only to avoid shadowing the query functions.
def _register_query_tools(mcp: FastMCP, pool: ConnectionPool) -> None:
    @mcp.tool(name="search_entities")
    def search_entities_tool(
        query: Annotated[str | None, Field(description="name to search for (fuzzy)")] = None,
        entity_type: EntityType | None = None,
        language: Annotated[str | None, Field(description="language code, e.g. en")] = None,
        category: Annotated[
            str | None, Field(description="podcast category, e.g. marketing")
        ] = None,
        outlet: Annotated[str | None, Field(description="journalist's outlet, exact name")] = None,
        active_within_days: Literal[7, 30, 90, 365] | None = None,
        has_contact: Annotated[bool | None, Field(description="has any contact route")] = None,
        first_seen: Literal["historical", "discovered"] | None = None,
        limit: Limit = 10,
    ) -> dict:
        """Search podcasts and journalists. Without a query, most recently active first.
        Each item carries last_activity_at with activity_basis (observed or historical)
        and last_checked_at (when this system last fetched it), plus a contact route count."""
        try:
            filters = parse_filters(
                q=query,
                entity_type=entity_type,
                language=language,
                category=category,
                outlet=outlet,
                active_within_days=active_within_days,
                has_contact=has_contact,
                first_seen=first_seen,
            )
            page = page_request(limit, 0, cap=MCP_MAX_RESULTS)
        except QueryInputError as exc:
            raise ToolError(str(exc)) from None
        with pool.connection() as conn:
            result = search_entities(conn, filters, page)
        return {"items": to_jsonable(result.items), "has_more": result.has_more}

    @mcp.tool(name="get_entity")
    def get_entity_tool(
        entity_id: Annotated[str, Field(description="entity UUID from search results")],
    ) -> dict:
        """One entity: identifiers, every source record (side by side, with dates),
        masked contact routes, the latest 50 observations, discoveries and pending matches."""
        try:
            eid = parse_entity_id(entity_id)
        except QueryInputError as exc:
            raise ToolError(str(exc)) from None
        with pool.connection() as conn:
            detail = get_entity(conn, eid)
        if detail is None:
            raise ToolError("no entity with that id")
        return to_jsonable(detail)


def _register_report_tools(mcp: FastMCP, pool: ConnectionPool) -> None:
    @mcp.tool
    def ping() -> str:
        """Health check for MCP clients."""
        return "pong"

    @mcp.tool(name="list_recent_discoveries")
    def list_recent_discoveries_tool(
        classification: Classification | None = None,
        source: DiscoverySource | None = None,
        entity_type: EntityType | None = None,
        limit: Limit = 20,
    ) -> dict:
        """Records first seen by discovery runs, newest first, with how each was classified
        and the existing entity it matched, if any."""
        page = page_request(limit, 0, cap=MCP_MAX_RESULTS)
        with pool.connection() as conn:
            result = list_recent_discoveries(
                conn,
                page,
                classification=classification,
                source=source,
                entity_type=entity_type,
            )
        return {"items": to_jsonable(result.items), "has_more": result.has_more}

    @mcp.tool(name="get_metrics")
    def get_metrics_tool() -> dict:
        """Entity counts, verification freshness per source, contact route coverage
        (counts only), discovery counts and the latest job runs."""
        with pool.connection() as conn:
            return to_jsonable(get_metrics(conn))


def build_mcp(pool: ConnectionPool, *, require_auth: bool = True) -> FastMCP:
    """require_auth=False is for in-process tests only; the app always passes True."""
    mcp = FastMCP(
        "data-discovery",
        instructions=INSTRUCTIONS,
        auth=ApiKeyVerifier(pool) if require_auth else None,
        middleware=[MeterMiddleware(pool)],
        mask_error_details=True,
    )
    _register_query_tools(mcp, pool)
    _register_report_tools(mcp, pool)
    return mcp
