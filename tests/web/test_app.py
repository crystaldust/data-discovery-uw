import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from ddm.config import WebConfig
from ddm.web.app import create_app
from tests import seed
from tests.web import world

pytestmark = pytest.mark.integration

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


def tool_call(name, arguments=None):
    return {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }


def bearer(key):
    return {**MCP_HEADERS, "Authorization": f"Bearer {key}"}


def test_healthz_is_public(client):
    response = client.get("/healthz")

    assert response.status_code == 200 and response.json() == {"status": "ok"}


def test_mcp_rejects_requests_without_a_key(client):
    assert client.post("/mcp/", json=MCP_INIT, headers=MCP_HEADERS).status_code == 401


def test_mcp_rejects_a_wrong_key(client):
    assert client.post("/mcp/", json=MCP_INIT, headers=bearer("ddm_wrong")).status_code == 401


def test_mcp_rejects_a_revoked_key(client, db, api_key):
    from ddm.access.keys import revoke_key

    revoke_key(db, api_key[0])

    assert client.post("/mcp/", json=MCP_INIT, headers=bearer(api_key[1])).status_code == 401


def test_mcp_accepts_a_live_key_and_meters_tool_calls(client, db, api_key):
    world.build(db)
    headers = bearer(api_key[1])

    init = client.post("/mcp/", json=MCP_INIT, headers=headers)
    call = client.post(
        "/mcp/", json=tool_call("search_entities", {"query": "marketing"}), headers=headers
    )
    usage = db.execute("SELECT surface, name, calls FROM api_usage").fetchall()

    assert init.status_code == 200 and call.status_code == 200
    assert "B2B Marketing Show" in call.text and seed.SECRET_EMAIL not in call.text
    assert usage == [("mcp", "search_entities", 1)]


def test_mcp_refuses_tool_calls_over_the_limit(client, db, fresh_minute):
    from ddm.access.keys import create_key

    _, key = create_key(db, "tight", rate_limit=1)
    headers = bearer(key)

    client.post("/mcp/", json=tool_call("ping"), headers=headers)
    refused = client.post("/mcp/", json=tool_call("ping"), headers=headers)

    assert "rate limit exceeded" in refused.text


def test_an_unreachable_database_gives_a_generic_503(web_config):
    dead = ConnectionPool(
        "postgresql://reader:pw@127.0.0.1:1/none",
        min_size=0,
        max_size=1,
        timeout=1,
        open=True,
        kwargs={"connect_timeout": 1},
    )
    try:
        with TestClient(create_app(web_config, pool=dead)) as client:
            response = client.get("/api/v1/metrics", headers={"Authorization": "Bearer ddm_x"})
    finally:
        dead.close()

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "unavailable"
    assert "127.0.0.1" not in response.text


def test_other_database_errors_are_a_generic_500(client, db, api_key):
    db.execute("REVOKE SELECT ON entity_search FROM ddm_reader")

    api = client.get("/api/v1/entities", headers={"Authorization": f"Bearer {api_key[1]}"})

    assert api.status_code == 500
    assert api.json() == {
        "error": {"code": "internal", "message": "the request could not be completed"}
    }
    assert "entity_search" not in api.text


def test_page_database_errors_are_a_generic_500_page(logged_in, db):
    db.execute("REVOKE SELECT ON entity_search FROM ddm_reader")

    page = logged_in.get("/")

    assert page.status_code == 500 and "could not be completed" in page.text
    assert "entity_search" not in page.text


def test_the_app_opens_and_closes_its_own_pool(reader_url, demo_password_hash, db):
    config = WebConfig(reader_url, "y" * 32, "demo", demo_password_hash, secure_cookies=False)

    with TestClient(create_app(config)) as client:
        assert client.get("/healthz").status_code == 200


def test_security_headers_are_on_every_response(client):
    page = client.get("/login")
    docs = client.get("/docs")

    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert "unsafe-inline" not in page.headers["content-security-policy"]
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["x-frame-options"] == "DENY"
    assert "'sha256-" in docs.headers["content-security-policy"]
    assert "cdn.jsdelivr.net" not in docs.headers["content-security-policy"] + docs.text
    assert "/static/swagger/swagger-ui-bundle.js" in docs.text


def test_static_files_are_served(client):
    assert client.get("/static/htmx.min.js").status_code == 200
    assert client.get("/static/pico.min.css").status_code == 200


def test_pages_and_api_are_not_cached_but_static_files_may_be(client):
    assert client.get("/login").headers["cache-control"] == "private, no-store"
    assert client.get("/api/v1/metrics").headers["cache-control"] == "private, no-store"
    assert "no-store" not in client.get("/static/htmx.min.js").headers.get("cache-control", "")


def test_mcp_is_served_without_the_trailing_slash(client, api_key):
    response = client.post(
        "/mcp", json=MCP_INIT, headers=bearer(api_key[1]), follow_redirects=False
    )

    assert response.status_code == 200


def test_unknown_mcp_tool_names_are_counted_in_one_bucket(client, db, api_key):
    headers = bearer(api_key[1])

    for name in ("no_such_tool_1", "no_such_tool_2"):
        client.post("/mcp/", json=tool_call(name), headers=headers)
    usage = db.execute("SELECT name, calls FROM api_usage").fetchall()

    assert usage == [("other", 2)]


def test_a_statement_timeout_is_reported_as_too_slow_not_as_an_outage(client, db, api_key):
    db.execute(
        "ALTER ROLE ddm_reader IN DATABASE "
        + db.execute("SELECT current_database()").fetchone()[0]
        + " SET statement_timeout = '1ms'"
    )
    client.app.state.pool.check()  # reconnect so the new default applies

    response = client.get(
        "/api/v1/entities",
        params={"q": "marketing"},
        headers={"Authorization": f"Bearer {api_key[1]}"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "query_timeout"


def test_a_database_failure_during_mcp_auth_is_not_reported_as_a_bad_key(web_config):
    dead = ConnectionPool(
        "postgresql://reader:pw@127.0.0.1:1/none",
        min_size=0,
        max_size=1,
        timeout=1,
        open=True,
        kwargs={"connect_timeout": 1},
    )
    try:
        with TestClient(create_app(web_config, pool=dead), raise_server_exceptions=False) as c:
            response = c.post("/mcp/", json=MCP_INIT, headers=bearer("ddm_anything"))
    finally:
        dead.close()

    assert response.status_code != 401
    assert "127.0.0.1" not in response.text
