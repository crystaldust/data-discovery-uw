import hashlib
from datetime import UTC, date, datetime

import pytest

from ddm.query.costs import (
    ACTIONS_PRIVATE_USD_PER_MINUTE,
    NEON_STORAGE_USD_PER_GB_MONTH,
    PRICE_SOURCES,
    get_costs,
)
from tests import seed

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 29, tzinfo=UTC)


def metrics(db, run_id, http_requests, bytes_fetched, duration_ms):
    db.execute(
        "INSERT INTO run_metrics (run_id, http_requests, bytes_fetched, duration_ms) "
        "VALUES (%s, %s, %s, %s)",
        (run_id, http_requests, bytes_fetched, duration_ms),
    )


def test_costs_sum_runs_per_source_inside_the_window(db, reader):
    metrics(db, seed.run(db, 2, "2026-10-27T00:00:00Z"), 300, 30 * 1024 * 1024, 600_000)
    metrics(db, seed.run(db, 2, "2026-10-28T00:00:00Z", status="partial"), 100, 0, 120_000)
    seed.run(db, 3, "2026-10-28T00:00:00Z", status="failed")  # no metrics row
    metrics(db, seed.run(db, 2, "2026-08-01T00:00:00Z"), 999, 0, 999_000)  # outside 30 days

    costs = get_costs(reader, now=NOW)
    rss = {s.source: s for s in costs.sources}["podcast_rss"]
    api = {s.source: s for s in costs.sources}["podcastindex_api"]

    assert (rss.runs, rss.ok, rss.partial, rss.failed) == (2, 1, 1, 0)
    assert (rss.http_requests, rss.mb_fetched, rss.minutes) == (400, 30.0, 12.0)
    assert (api.runs, api.failed, api.http_requests) == (1, 1, 0)


def test_resources_compare_use_with_free_tiers_and_estimate_paid_cost(db, reader):
    metrics(db, seed.run(db, 2, "2026-10-27T00:00:00Z"), 1, 0, 30 * 60_000)

    resources = {r.resource: r for r in get_costs(reader, now=NOW).resources}
    storage = resources["Neon storage"]
    actions = resources["GitHub Actions minutes"]

    assert storage.used > 0 and 0 < storage.share_of_free < 1
    assert storage.paid_estimate_usd == round(
        storage.used / 1024 * NEON_STORAGE_USD_PER_GB_MONTH, 2
    )
    assert actions.used == 30.0 and actions.free_limit is None
    assert actions.paid_estimate_usd == round(30 * ACTIONS_PRIVATE_USD_PER_MINUTE, 2)
    assert "non-commercial" in resources["Vercel function calls"].note


def test_api_calls_are_counted_per_day_and_surface(db, reader):
    key_id = db.execute(
        "INSERT INTO api_keys (label, key_hash) VALUES ('t', %s) RETURNING key_id",
        (hashlib.sha256(b"costs").digest(),),
    ).fetchone()[0]
    db.execute(
        "INSERT INTO api_usage VALUES (%s, '2026-10-28 10:00Z', 'api', 'entities', 4),"
        " (%s, '2026-10-28 10:00Z', 'mcp', 'ping', 2), (%s, '2026-07-01 10:00Z', 'api', 'x', 9)",
        (key_id, key_id, key_id),
    )

    costs = get_costs(reader, now=NOW)

    assert [(c.day, c.surface, c.calls) for c in costs.api_calls] == [
        (date(2026, 10, 28), "api", 4),
        (date(2026, 10, 28), "mcp", 2),
    ]
    assert {r.resource: r for r in costs.resources}["Vercel function calls"].used == 6


def test_an_empty_database_costs_nothing_and_cites_its_prices(reader):
    costs = get_costs(reader, now=NOW)

    assert costs.sources == () and costs.api_calls == ()
    assert costs.within_free_tiers is True
    assert costs.price_sources == PRICE_SOURCES and costs.prices_as_of == "2026-10-29"


def test_reaching_a_free_limit_is_reported(db, reader, monkeypatch):
    monkeypatch.setattr("ddm.query.costs.NEON_FREE_STORAGE_BYTES", 1)

    costs = get_costs(reader, now=NOW)

    assert costs.within_free_tiers is False
