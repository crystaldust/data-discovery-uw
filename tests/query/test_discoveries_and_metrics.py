from datetime import UTC, datetime

import pytest

from ddm.query.discoveries import list_recent_discoveries
from ddm.query.filters import QueryInputError, page_request
from ddm.query.metrics import RECENT_RUNS, get_metrics
from tests import seed

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 29, tzinfo=UTC)


def discovered_world(db):
    base = seed.run(db, 1, "2026-10-01T00:00:00Z", kind="historical_backfill")
    known = seed.entity(db, "podcast", "Known Show", base)
    seed.record(db, 1, "1", known, base, {})
    run = seed.run(db, 3, "2026-10-28T00:00:00Z", kind="discovery")
    new = seed.entity(db, "podcast", "Brand New", run, kind="discovered")
    seed.record(db, 3, "10", new, run, {}, provenance="observed")
    seed.record(db, 3, "11", known, run, {}, provenance="observed")
    seed.discovery(db, run, 3, "10", "new", new)
    seed.discovery(
        db, run, 3, "11", "duplicate", known, matched=known, method="feed_url", score=1.0
    )
    seed.route(db, new, "feed_owner_email", seed.SECRET_EMAIL, run, source_id=3, source_key="10")
    seed.candidate(db, 3, "10", known, run)
    return known, new


def test_discoveries_are_listed_with_names_and_matches(db, reader):
    known, new = discovered_world(db)

    page = list_recent_discoveries(reader, page_request(10))

    assert [(d.entity_name, d.classification, d.matched_name) for d in page.items] == [
        ("Brand New", "new", None),
        ("Known Show", "duplicate", "Known Show"),
    ]
    assert page.items[0].source == "podcastindex_api"


def test_discoveries_can_be_filtered_and_paged(db, reader):
    discovered_world(db)

    only_new = list_recent_discoveries(reader, page_request(10), classification="new")
    journalists = list_recent_discoveries(reader, page_request(10), entity_type="journalist")
    first = list_recent_discoveries(reader, page_request(1), source="podcastindex_api")

    assert [d.entity_name for d in only_new.items] == ["Brand New"]
    assert journalists.items == ()
    assert len(first.items) == 1 and first.has_more


@pytest.mark.parametrize("field", ["classification", "source", "entity_type"])
def test_discovery_filters_refuse_unknown_values(reader, field):
    with pytest.raises(QueryInputError, match=field):
        list_recent_discoveries(reader, page_request(10), **{field: "bogus"})


def test_metrics_count_entities_freshness_routes_and_discoveries(db, reader):
    discovered_world(db)
    seed.refresh(db)

    metrics = get_metrics(reader, now=NOW)

    assert {(c.entity_type, c.first_seen_kind): c.entities for c in metrics.entities} == {
        ("podcast", "historical"): 1,
        ("podcast", "discovered"): 1,
    }
    fresh = {f.source: f for f in metrics.freshness}
    assert fresh["podcastindex_dump"].records == 1 and fresh["podcastindex_dump"].verified_30d == 0
    assert fresh["podcastindex_api"].verified_7d == 2
    assert [(r.route_type, r.routes) for r in metrics.contact_routes] == [("feed_owner_email", 1)]
    assert {(d.classification, d.records) for d in metrics.discoveries} == {
        ("new", 1),
        ("duplicate", 1),
    }
    assert metrics.pending_candidates == 1
    assert [r.source for r in metrics.recent_runs] == ["podcastindex_api", "podcastindex_dump"]
    assert metrics.db_size_bytes > 0
    assert metrics.search_refreshed_at is not None
    assert seed.SECRET_EMAIL not in repr(metrics)


def test_metrics_show_only_the_most_recent_runs(db, reader):
    for _ in range(RECENT_RUNS + 3):
        seed.run(db, 1)

    assert len(get_metrics(reader, now=NOW).recent_runs) == RECENT_RUNS


def test_metrics_of_an_empty_database_are_zero(reader):
    metrics = get_metrics(reader, now=NOW)

    assert metrics.entities == () and metrics.freshness == () and metrics.recent_runs == ()
    assert metrics.pending_candidates == 0
    assert metrics.search_refreshed_at is None
