"""FastAPI application with a read-only MCP endpoint mounted at /mcp."""

import logging

import psycopg
from fastapi import FastAPI
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

logger = logging.getLogger(__name__)

DB_CONNECT_TIMEOUT_SECONDS = 10


def build_mcp(token: str | None, readonly_db_url: str | None) -> FastMCP:
    """Build the MCP server. A None token disables auth; only tests use that."""
    auth = (
        StaticTokenVerifier(tokens={token: {"client_id": "demo", "scopes": []}}) if token else None
    )
    mcp = FastMCP("data-discovery", auth=auth)

    @mcp.tool
    def ping() -> str:
        """Health check for MCP clients."""
        return "pong"

    @mcp.tool
    def db_version() -> str:
        """Return the PostgreSQL server version, read through the read-only role."""
        if not readonly_db_url:
            return "database not configured"
        with psycopg.connect(readonly_db_url, connect_timeout=DB_CONNECT_TIMEOUT_SECONDS) as conn:
            row = conn.execute("select version()").fetchone()
        return row[0] if row else "unknown"

    return mcp


def create_app(token: str, readonly_db_url: str | None) -> FastAPI:
    mcp_app = build_mcp(token, readonly_db_url).http_app(path="/", stateless_http=True)
    app = FastAPI(title="Data Discovery Demo", lifespan=mcp_app.lifespan)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.mount("/mcp", mcp_app)
    return app
