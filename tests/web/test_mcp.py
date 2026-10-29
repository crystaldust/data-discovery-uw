import asyncio
import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from ddm.access.keys import create_key
from ddm.web.mcp import build_mcp
from tests import seed

pytestmark = pytest.mark.integration


def world(db):
    run = seed.run(db, 2)
    show = seed.entity(db, "podcast", "B2B Marketing Show", run)
    seed.record(
        db,
        2,
        "1",
        show,
        run,
        {
            "language": "en",
            "categories": ["marketing"],
            "last_episode_at": "2026-10-20T00:00:00+00:00",
            "owner_email": seed.SECRET_EMAIL,
        },
        provenance="observed",
    )
    seed.route(db, show, "feed_owner_email", seed.SECRET_EMAIL, run, source_key="1")
    seed.discovery(db, run, 2, "1", "new", show)
    seed.refresh(db)
    return show


def call(pool, tool, arguments=None):
    async def run():
        async with Client(build_mcp(pool, require_auth=False)) as client:
            return (await client.call_tool(tool, arguments or {})).data

    return asyncio.run(run())


def test_the_server_lists_four_query_tools_and_ping(pool):
    async def run():
        async with Client(build_mcp(pool, require_auth=False)) as client:
            return sorted(t.name for t in await client.list_tools())

    assert asyncio.run(run()) == [
        "get_entity",
        "get_metrics",
        "list_recent_discoveries",
        "ping",
        "search_entities",
    ]


def test_search_returns_items_without_emails(db, pool):
    world(db)

    result = call(pool, "search_entities", {"category": "marketing", "active_within_days": 30})

    assert [i["display_name"] for i in result["items"]] == ["B2B Marketing Show"]
    assert result["items"][0]["activity_basis"] == "observed"
    assert seed.SECRET_EMAIL not in json.dumps(result)


def test_get_entity_returns_masked_routes(db, pool):
    show = world(db)

    result = call(pool, "get_entity", {"entity_id": str(show)})

    assert result["contact_routes"][0]["display_value"] == "j***@example.org"
    assert seed.SECRET_EMAIL not in json.dumps(result)


def test_discoveries_and_metrics_answer(db, pool):
    world(db)

    discoveries = call(pool, "list_recent_discoveries", {"classification": "new"})
    metrics = call(pool, "get_metrics")

    assert discoveries["items"][0]["entity_name"] == "B2B Marketing Show"
    assert metrics["pending_candidates"] == 0
    assert seed.SECRET_EMAIL not in json.dumps(metrics)


def test_more_than_fifty_results_are_refused(pool):
    with pytest.raises(ToolError):
        call(pool, "search_entities", {"limit": 51})


def test_a_bad_entity_id_is_a_tool_error_not_a_crash(pool):
    with pytest.raises(ToolError, match="entity_id"):
        call(pool, "get_entity", {"entity_id": "not-a-uuid"})


def test_an_unknown_entity_is_a_tool_error(pool):
    with pytest.raises(ToolError, match="no entity"):
        call(pool, "get_entity", {"entity_id": "00000000-0000-0000-0000-000000000000"})


def test_an_overlong_query_is_refused(pool):
    with pytest.raises(ToolError, match="q is longer"):
        call(pool, "search_entities", {"query": "x" * 201})


def test_tools_without_a_token_are_not_metered(db, pool):
    create_key(db, "demo")

    call(pool, "ping")

    assert db.execute("SELECT count(*) FROM api_usage").fetchone()[0] == 0
