from datetime import UTC, datetime, timedelta

import pytest

from ddm.db.migrate import migrate
from ddm.ingest.byline_classify import BYLINE_FUZZY_METHOD
from ddm.ingest.bylines import MAX_FETCH_SECONDS, BylineInputError, sync_bylines
from ddm.ingest.wikidata_sync import sync_journalists
from ddm.sources.outlet_rss import FEED_KEY_PREFIX
from tests.ingest.test_refresh import FAILED, NOT_MODIFIED, FakeFetcher
from tests.ingest.test_wikidata_sync import FakeClient, detail, person

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)
ATLANTIC, NYT, POLITICO = "Q1542536", "Q9684", "Q3109740"
AVERY_FEED = "https://www.theatlantic.com/feed/author/avery-example/"
BLAKE_FEED = "https://www.theatlantic.com/feed/author/blake-sample/"
ATLANTIC_ALL = "https://www.theatlantic.com/feed/all/"
NYT_HOME = "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"
POLITICO_FEED = "https://rss.politico.com/politics-news.xml"
TECHCRUNCH = "https://techcrunch.com/feed/"
AVERY_PAGE = "theatlantic.com/author/avery-example"


def atom_item(name, slug, link):
    return (
        f"<item><title>{link}</title><link>https://www.theatlantic.com/{link}</link>"
        "<pubDate>Tue, 27 Oct 2026 12:00:00 +0000</pubDate>"
        f"<atom:author><atom:name>{name}</atom:name>"
        f"<atom:uri>https://www.theatlantic.com/author/{slug}/</atom:uri></atom:author></item>"
    )


def dc_item(creator, link):
    return (
        f"<item><title>{link}</title><link>https://news.example.com/{link}</link>"
        f"<pubDate>Tue, 27 Oct 2026 08:00:00 +0000</pubDate><dc:creator>{creator}</dc:creator>"
        "</item>"
    )


def feed(*items):
    return (
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>'
        f"{''.join(items)}</channel></rss>"
    ).encode()


BODIES = {
    AVERY_FEED: feed(atom_item("Avery Example", "avery-example", "essay")),
    BLAKE_FEED: feed(
        atom_item("Blake Sample", "blake-sample", "review"),
        dc_item("Blake Sample and Erin Coauthor", "joint"),
    ),
    ATLANTIC_ALL: feed(),
    NYT_HOME: feed(dc_item("Casey Writer", "casey")),
    POLITICO_FEED: feed(dc_item("The Associated Press", "wire")),
    TECHCRUNCH: feed(dc_item("Dana Newperson", "gadget")),
}
# Avery: Wikidata knows them at NYT only, but they have an Atlantic author id.
PEOPLE = {
    NYT: [person("Q1", "Avery Example")],
    ATLANTIC: [person("Q2", "Blake Sample")],
    POLITICO: [person("Q3", "Casey Writer")],
}
DETAILS = [detail("Q1", "P6791", "avery-example"), detail("Q2", "P6791", "blake-sample")]


@pytest.fixture
def db(fresh_db):
    migrate(fresh_db)
    sync_journalists(
        fresh_db, FakeClient(PEOPLE, DETAILS), now=lambda: NOW, refused_backoff=timedelta(days=30)
    )
    return fresh_db


def fetcher(bodies=None, **kwargs):
    found = FakeFetcher(bodies or BODIES, **kwargs)
    found.clock = NOW
    return found


def run(conn, fetch, *, now=NOW, author_feeds=50, monotonic=None):
    ticks = iter(range(10_000))
    return sync_bylines(
        conn,
        fetch,
        author_feeds=author_feeds,
        now=lambda: now,
        monotonic=monotonic or (lambda: float(next(ticks))),
    )


def discovery(conn, key):
    return conn.execute(
        "select classification, entity_id, matched_entity_id, method from discoveries"
        " where source_id = 5 and source_key = %s",
        (key,),
    ).fetchone()


def wikidata_entity(conn, qid):
    return conn.execute(
        "select entity_id from source_records where source_id = 4 and source_key = %s", (qid,)
    ).fetchone()[0]


def test_an_author_page_byline_at_a_new_outlet_is_the_same_person_moving(db):
    report = run(db, fetcher())
    avery = wikidata_entity(db, "Q1")

    kind, entity_id, matched, _ = discovery(db, AVERY_PAGE)

    assert report.status == "ok"
    assert (kind, entity_id, matched) == ("rename_or_move", avery, avery)
    sources = db.execute(
        "select source_id from source_records where entity_id = %s order by 1", (avery,)
    ).fetchall()
    assert sources == [(4,), (5,)]
    observed = db.execute(
        "select distinct source_id from observations o join source_records r using"
        " (source_id, source_key) where r.entity_id = %s order by 1",
        (avery,),
    ).fetchall()
    assert observed == [(4,), (5,)]


def test_an_author_page_byline_at_a_known_outlet_is_a_duplicate(db):
    run(db, fetcher())

    kind, entity_id, _, _ = discovery(db, "theatlantic.com/author/blake-sample")

    assert kind == "duplicate"
    assert entity_id == wikidata_entity(db, "Q2")


def test_a_same_name_byline_without_a_page_is_queued_never_merged(db):
    run(db, fetcher())
    casey = wikidata_entity(db, "Q3")

    kind, entity_id, matched, method = discovery(db, f"{NYT}:casey writer")

    assert (kind, matched, method) == ("uncertain", casey, BYLINE_FUZZY_METHOD)
    assert entity_id != casey
    queued = db.execute(
        "select method from candidate_matches where source_key = %s", (f"{NYT}:casey writer",)
    ).fetchall()
    assert queued == [(BYLINE_FUZZY_METHOD,)]


def test_a_byline_like_nobody_is_a_new_discovered_journalist(db):
    run(db, fetcher())

    kind, entity_id, _, _ = discovery(db, "Q1022366:dana newperson")

    assert kind == "new"
    row = db.execute(
        "select entity_type, first_seen_kind from entities where entity_id = %s", (entity_id,)
    ).fetchone()
    assert row == ("journalist", "discovered")


def test_a_co_author_in_an_author_feed_does_not_get_that_page(db):
    run(db, fetcher())

    identifiers = db.execute(
        "select count(*) from entity_identifiers i join source_records r on"
        " r.entity_id = i.entity_id where r.source_key = %s",
        (f"{ATLANTIC}:erin coauthor",),
    ).fetchone()[0]

    assert discovery(db, f"{ATLANTIC}:erin coauthor")[0] == "new"
    assert identifiers == 0


def test_organisation_bylines_are_counted_not_kept(db):
    report = run(db, fetcher())

    assert report.organisation_bylines == 1
    assert report.authors == 5
    found = db.execute("select count(*) from source_records where source_key like %s", ("%press%",))
    assert found.fetchone()[0] == 0


def test_the_author_page_is_a_confirmed_route_from_the_outlet_feed(db):
    run(db, fetcher())

    route = db.execute(
        "select affiliation_status, affiliation_basis from contact_routes"
        " where source_id = 5 and value = %s",
        (f"https://{AVERY_PAGE}",),
    ).fetchone()

    assert route == ("confirmed", "byline in the outlet's own feed")


def test_the_same_feeds_again_write_no_discoveries_or_observations(db):
    run(db, fetcher())

    again = run(db, fetcher(), now=NOW + timedelta(hours=1))

    assert (
        db.execute(
            "select count(*) from discoveries where run_id = %s", (again.run_id,)
        ).fetchone()[0]
        == 0
    )
    assert (
        db.execute(
            "select count(*) from observations where run_id = %s", (again.run_id,)
        ).fetchone()[0]
        == 0
    )


def test_a_refused_feed_makes_the_run_partial_and_the_others_still_count(db):
    report = run(db, fetcher(overrides={NYT_HOME: {**FAILED, "status": 403, "reason": "http_403"}}))

    assert report.status == "partial"
    assert report.outcomes["failed"] == 1
    assert discovery(db, AVERY_PAGE)[0] == "rename_or_move"


def test_a_feed_that_does_not_parse_is_rejected_alone(db):
    report = run(db, fetcher({**BODIES, TECHCRUNCH: b"<html><body>no</body></html>"}))

    assert report.status == "ok"
    assert report.outcomes["rejected"] == 1
    reason = db.execute(
        "select reason from import_rejects where run_id = %s", (report.run_id,)
    ).fetchone()
    assert reason == ("unsupported_format",)
    assert discovery(db, AVERY_PAGE) is not None


def test_a_fetcher_crash_is_one_failed_feed(db):
    report = run(db, fetcher(fail_on=TECHCRUNCH))

    assert (report.status, report.outcomes["failed"]) == ("partial", 1)


def test_fetch_log_keys_carry_the_outlet_prefix(db):
    report = run(db, fetcher())

    keys = db.execute(
        "select source_key from fetch_attempts where run_id = %s", (report.run_id,)
    ).fetchall()
    assert len(keys) == 6
    assert all(k.startswith(FEED_KEY_PREFIX) for (k,) in keys)


def test_author_feeds_are_capped_least_recently_attempted_first(db):
    first = fetcher()
    run(db, first, author_feeds=1)
    second = fetcher()

    run(db, second, author_feeds=1, now=NOW + timedelta(hours=1))

    first_authors = {u for u, *_ in first.calls if "/author/" in u}
    second_authors = {u for u, *_ in second.calls if "/author/" in u}
    assert len(first_authors) == len(second_authors) == 1
    assert first_authors != second_authors


def test_fetching_stops_when_the_time_budget_is_spent(db):
    clock = iter([0.0, 0.0, MAX_FETCH_SECONDS + 1] + [MAX_FETCH_SECONDS + 2] * 100)

    report = run(db, fetcher(), monotonic=lambda: next(clock))

    assert sum(report.outcomes.values()) == 1


@pytest.mark.parametrize("bad", [0, 201])
def test_the_author_feed_count_is_bounded(db, bad):
    with pytest.raises(BylineInputError):
        run(db, fetcher(), author_feeds=bad)


def test_an_unexpected_parser_error_rejects_only_that_feed(db, monkeypatch):
    import ddm.ingest.bylines as bylines

    real = bylines.parse_bylines

    def exploding(body, fetched_at):
        if b"Dana Newperson" in body:
            raise RecursionError("deep")
        return real(body, fetched_at)

    monkeypatch.setattr(bylines, "parse_bylines", exploding)

    report = run(db, fetcher())

    assert report.outcomes["rejected"] == 1
    assert discovery(db, AVERY_PAGE)[0] == "rename_or_move"


def test_a_stored_window_is_kept_when_a_feed_answers_304(db):
    run(db, fetcher())
    avery_again = feed(atom_item("Avery Example", "avery-example", "second-essay"))
    later = fetcher({**BODIES, ATLANTIC_ALL: avery_again}, overrides={AVERY_FEED: NOT_MODIFIED})

    run(db, later, now=NOW + timedelta(hours=1))

    bylines = db.execute(
        "select attrs->'bylines' from source_records where source_id = 5 and source_key = %s",
        (AVERY_PAGE,),
    ).fetchone()[0]
    assert sorted(b["title"] for b in bylines) == ["essay", "second-essay"]


def test_an_ambiguous_author_page_gets_no_author_feed(fresh_db):
    migrate(fresh_db)
    people = {ATLANTIC: [person("Q2", "Blake Sample"), person("Q9", "Blake Sample")]}
    shared = [detail("Q2", "P6791", "blake-sample"), detail("Q9", "P6791", "blake-sample")]
    sync_journalists(
        fresh_db, FakeClient(people, shared), now=lambda: NOW, refused_backoff=timedelta(days=30)
    )
    seen = fetcher()

    run(fresh_db, seen)

    assert BLAKE_FEED not in {url for url, *_ in seen.calls}
