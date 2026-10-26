from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from ddm.db.migrate import migrate
from ddm.ingest.backoff import DiscoveryBackoff
from ddm.ingest.baseline import import_baseline
from ddm.ingest.discover_dump import DiscoveryInputError
from ddm.ingest.discover_newfeeds import (
    DISCOVERY_LOCK_KEY,
    LISTINGS_PER_LOOKUP,
    MAX_CONSECUTIVE_FAILURES,
    DiscoveryBusy,
    discover_newfeeds,
)
from ddm.sources.podcastindex import SourceAccessDenied, SourceResponseError, SourceUnavailable
from tests.conftest import dump_row, make_dump

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 30, tzinfo=UTC)
SETTLED = int((NOW - timedelta(days=3)).timestamp())


def listing(feed_id, language="en", added=SETTLED, dead=0):
    return {"id": feed_id, "language": language, "timeAdded": added, "dead": dead}


def api_feed(feed_id, **overrides):
    return {
        "id": feed_id,
        "title": f"Api Show {feed_id}",
        "url": f"https://api.example.com/{feed_id}.xml",
        "language": "en",
        "episodeCount": 5,
        "categories": {"1": "Technology"},
        "lastParseTime": 1790000000,
        "lastUpdateTime": 1790000000,
        "podcastGuid": "",
        "itunesId": 0,
        **overrides,
    }


class FakeApi:
    """Pages like the real API (ids >= feedid, ascending) and records every call."""

    def __init__(self, listings, feeds, refuse=(), fail=None, retry_after=None):
        self.listings = sorted(listings, key=lambda f: f["id"])
        self.feeds = feeds
        self.refuse = set(refuse)
        self.fail = fail or {}  # feed id -> exception raised by its lookup
        self.retry_after = retry_after
        self.calls = []

    def get(self, path, params):
        self.calls.append((path, dict(params)))
        if path == "/recent/newfeeds":
            page = [f for f in self.listings if f["id"] >= params["feedid"]]
            return {"status": True, "feeds": page[: params["max"]]}
        feed_id = params["id"]
        if feed_id in self.refuse:
            raise SourceAccessDenied(429 if self.retry_after else 403, self.retry_after, path)
        if feed_id in self.fail:
            raise self.fail[feed_id]
        return {"status": "true", "feed": self.feeds.get(feed_id, [])}

    def looked_up(self):
        return [p["id"] for path, p in self.calls if path == "/podcasts/byfeedid"]


@pytest.fixture
def db(fresh_db, tmp_path):
    migrate(fresh_db)
    rows = [dump_row(k) for k in range(1, 9)] + [dump_row(9, podcastGuid="g9", title="Nine")]
    import_baseline(fresh_db, make_dump(tmp_path, rows))
    return fresh_db


def run(conn, api, **kwargs):
    return discover_newfeeds(conn, api, now=lambda: NOW, **kwargs)


def classes(conn, run_id):
    rows = conn.execute(
        "select source_key, classification from discoveries where run_id = %s", (run_id,)
    ).fetchall()
    return dict(rows)


def cursor(conn):
    row = conn.execute("select last_feed_id from discovery_cursors where source_id = 3").fetchone()
    return row[0] if row else None


def test_first_run_starts_at_the_newest_dump_feed_and_filters_before_looking_up(db):
    api = FakeApi(
        [listing(9), listing(100), listing(101, language="es"), listing(102), listing(103)],
        {
            9: api_feed(9, podcastGuid="g9", title="Nine", url="https://feeds.example.com/9.xml"),
            100: api_feed(100),
            102: api_feed(102, podcastGuid="g9", title="Nine"),  # the dump's feed 9 moved here
            103: api_feed(103, episodeCount=1),  # outside the demo slice
        },
    )

    report = run(db, api)

    assert api.calls[0] == ("/recent/newfeeds", {"feedid": 9, "max": 1000})
    assert api.looked_up() == [9, 100, 102, 103]
    assert classes(db, report.run_id) == {"9": "duplicate", "100": "new", "102": "rename_or_move"}
    m = report.metrics
    assert (m.rows_in, m.rows_skipped, m.rows_rejected) == (4, 2, 0)
    assert m.http_requests == len(api.calls)
    assert (report.status, cursor(db)) == ("ok", 103)


def test_the_next_run_continues_after_the_cursor(db):
    api = FakeApi([listing(9), listing(100)], {9: api_feed(9), 100: api_feed(100)})
    run(db, api)
    api.listings.append(listing(104))
    api.feeds[104] = api_feed(104)
    api.calls.clear()

    report = run(db, api)

    assert api.calls[0][1]["feedid"] == 101
    assert api.looked_up() == [104]
    assert classes(db, report.run_id) == {"104": "new"}
    assert cursor(db) == 104


def test_listings_too_young_to_be_parsed_stop_the_run_before_the_cursor_passes_them(db):
    young = int((NOW - timedelta(hours=1)).timestamp())
    api = FakeApi([listing(100), listing(101, added=young), listing(102)], {100: api_feed(100)})

    report = run(db, api)

    assert api.looked_up() == [100]
    assert (report.stopped, cursor(db)) == ("unsettled", 100)


def test_a_refusal_keeps_committed_batches_and_marks_the_run_partial(db):
    api = FakeApi(
        [listing(100), listing(101), listing(102)],
        {100: api_feed(100), 102: api_feed(102)},
        refuse={101},
    )

    report = run(db, api, batch_size=1)

    assert (report.status, report.stopped) == ("partial", "refused")
    assert classes(db, report.run_id) == {"100": "new"}
    assert cursor(db) == 100
    assert 102 not in api.looked_up()


def test_limit_caps_the_english_feeds_looked_up(db):
    api = FakeApi([listing(100), listing(101), listing(102)], {k: api_feed(k) for k in (100, 101)})

    report = run(db, api, limit=2)

    assert api.looked_up() == [100, 101]
    assert (report.stopped, cursor(db)) == ("limit", 101)


def test_unparsed_and_vanished_feeds_are_rejected_and_passed(db):
    api = FakeApi(
        [listing(100), listing(101)],
        {100: api_feed(100, title="", lastParseTime=0)},  # 101 is unknown to byfeedid
    )

    report = run(db, api)

    reasons = db.execute(
        "select source_key, reason from import_rejects where run_id = %s order by row_number",
        (report.run_id,),
    ).fetchall()
    assert reasons == [("100", "not_parsed"), ("101", "not_found")]
    assert cursor(db) == 101


def test_a_second_discovery_while_one_runs_is_refused(db, pg_url):
    with psycopg.connect(pg_url, autocommit=True) as other:
        other.execute("select pg_advisory_lock(%s)", (DISCOVERY_LOCK_KEY,))

        with pytest.raises(DiscoveryBusy):
            run(db, FakeApi([], {}))


def test_limit_must_be_in_range(db):
    with pytest.raises(DiscoveryInputError):
        run(db, FakeApi([], {}), limit=0)


def test_a_crashed_run_is_marked_failed_by_the_next_one(db):
    first = run(db, FakeApi([listing(100)], {100: api_feed(100)}))
    db.execute(
        "update import_runs set status = 'running', finished_at = null where run_id = %s",
        (first.run_id,),
    )

    run(db, FakeApi([], {}))

    status = db.execute(
        "select status from import_runs where run_id = %s", (first.run_id,)
    ).fetchone()[0]
    assert status == "failed"


def test_one_feed_that_cannot_be_looked_up_is_rejected_and_passed(db):
    api = FakeApi(
        [listing(100), listing(101), listing(102)],
        {100: api_feed(100), 102: api_feed(102)},
        fail={101: SourceResponseError("status=400 path=/podcasts/byfeedid")},
    )

    report = run(db, api)

    assert classes(db, report.run_id) == {"100": "new", "102": "new"}
    reason = db.execute(
        "select reason from import_rejects where run_id = %s and source_key = '101'",
        (report.run_id,),
    ).fetchone()
    assert (reason, report.status, cursor(db)) == (("lookup_failed",), "ok", 102)


def test_failures_in_a_row_mean_the_source_is_down_and_stop_before_them(db):
    down = range(101, 101 + MAX_CONSECUTIVE_FAILURES)
    api = FakeApi(
        [listing(100), *(listing(k) for k in down), listing(200)],
        {100: api_feed(100), 200: api_feed(200)},
        fail={k: SourceUnavailable("down") for k in down},
    )

    report = run(db, api)

    assert (report.status, report.stopped, cursor(db)) == ("partial", "unavailable", 100)
    assert 200 not in api.looked_up()
    rejected = db.execute(
        "select count(*) from import_rejects where run_id = %s", (report.run_id,)
    ).fetchone()[0]
    assert rejected == 0


def test_a_refusal_is_remembered_and_the_next_run_waits(db):
    api = FakeApi([listing(100)], {}, refuse={100}, retry_after="3600")
    run(db, api)
    stored = db.execute(
        "select retry_after, http_status from source_backoff where source_id = 3"
    ).fetchone()
    assert stored == (NOW + timedelta(hours=1), 429)

    with pytest.raises(DiscoveryBackoff, match="429"):
        run(db, FakeApi([], {}))

    later = discover_newfeeds(db, FakeApi([], {}), now=lambda: NOW + timedelta(hours=2))
    assert later.status == "ok"


def test_a_refusal_without_retry_after_uses_the_configured_backoff(db):
    run(db, FakeApi([listing(100)], {}, refuse={100}), refused_backoff=timedelta(days=3))

    retry_after = db.execute(
        "select retry_after from source_backoff where source_id = 3"
    ).fetchone()[0]
    assert retry_after == NOW + timedelta(days=3)


def test_listings_without_a_time_added_are_skipped_not_waited_for(db):
    api = FakeApi([listing(100, added=None), listing(101)], {101: api_feed(101)})

    report = run(db, api)

    assert api.looked_up() == [101]
    assert (report.stopped, report.metrics.rows_skipped, cursor(db)) == ("caught_up", 1, 101)


def test_a_run_scans_a_bounded_number_of_listings(db):
    cap = 1 * LISTINGS_PER_LOOKUP
    api = FakeApi([listing(100 + k, language="es") for k in range(cap + 5)], {})

    report = run(db, api, limit=1)

    assert (report.stopped, cursor(db)) == ("listing_cap", 100 + cap - 1)
