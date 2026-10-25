import asyncio

from fastapi.testclient import TestClient
from fastmcp import Client

from ddm.web.app import build_mcp, create_app

TOKEN = "test-token"
MCP_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def call_tool(name: str, readonly_db_url=None):
    async def run():
        async with Client(build_mcp(token=None, readonly_db_url=readonly_db_url)) as client:
            result = await client.call_tool(name, {})
            return result.data

    return asyncio.run(run())


def test_healthz_is_public():
    with TestClient(create_app(token=TOKEN, readonly_db_url=None)) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_mcp_rejects_requests_without_a_token():
    with TestClient(create_app(token=TOKEN, readonly_db_url=None)) as client:
        response = client.post("/mcp/", json=MCP_INIT, headers=MCP_HEADERS)

    assert response.status_code == 401


def test_mcp_rejects_a_wrong_token():
    headers = {**MCP_HEADERS, "Authorization": "Bearer wrong"}
    with TestClient(create_app(token=TOKEN, readonly_db_url=None)) as client:
        response = client.post("/mcp/", json=MCP_INIT, headers=headers)

    assert response.status_code == 401


def test_mcp_accepts_the_configured_token():
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"}
    with TestClient(create_app(token=TOKEN, readonly_db_url=None)) as client:
        response = client.post("/mcp/", json=MCP_INIT, headers=headers)

    assert response.status_code == 200


def test_ping_tool_answers_pong():
    assert call_tool("ping") == "pong"


def test_db_version_reports_when_database_is_not_configured():
    assert call_tool("db_version") == "database not configured"
