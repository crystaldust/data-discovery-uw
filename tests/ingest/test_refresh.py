import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from ddm.db.migrate import migrate
from ddm.ingest.baseline import import_baseline
from ddm.ingest.fetch_log import RefreshInputError
from ddm.ingest.refresh import REFRESH_LOCK_KEY, RefreshBusy, refresh_podcasts
from ddm.ingest.runs import read_metrics, register_run
from ddm.sources.rss_fetch import FetchResult
from tests.conftest import dump_row, make_dump

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
HTML = (Path(__file__).parent.parent / "fixtures" / "rss" / "html_page.html").read_bytes()
EPISODE = "Fri, 02 Oct 2026 10:00:00 +0000"
NEWER_EPISODE = "Fri, 23 Oct 2026 10:00:00 +0000"


def url(key):
    return f"https://feeds.example.com/{key}.xml"


def rss(title, *, guid=None, episode=EPISODE, owner=None, link=None):
    parts = [f"<title>{title}</title>", f"<item><pubDate>{episode}</pubDate></item>"]
    if guid:
        parts.append(f"<podcast:guid>{guid}</podcast:guid>")
    if owner:
        parts.append(f"<itunes:owner><itunes:email>{owner}</itunes:email></itunes:owner>")
    if link:
        parts.append(f"<link>{link}</link>")
    return (
        '<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"'
        ' xmlns:podcast="https://podcastindex.org/namespace/1.0"><channel>'
        f"{''.join(parts)}</channel></rss>"
    ).encode()


def bodies(**changed):
    default = {
        "1": rss(
            "Show 1", guid="guid-1", owner="owner1@example.com", link="https://one.example.com"
        ),
        "2": rss("Show 2", guid="guid-2"),
        "3": rss("Show 3"),
        "4": rss("Show 4"),
    }
    return {url(k): v for k, v in {**default, **changed}.items()}


class FakeFetcher:
    def __init__(self, feed_bodies, overrides=None, fail_on=None):
        self.bodies = feed_bodies
        self.overrides = overrides or {}
        self.fail_on = fail_on
        self.calls = []
        self.clock = T0

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def fetch(self, feed_url, *, etag, last_modified):
        self.calls.append((feed_url, etag, last_modified))
        self.clock += timedelta(seconds=1)
        if feed_url == self.fail_on:
            raise RuntimeError("fetcher exploded")
        body = self.bodies.get(feed_url, b"")
        result = FetchResult(
            requested_url=feed_url,
            final_url=feed_url,
            outcome="fetched",
            reason=None,
            status=200,
            body=body,
            content_type="application/rss+xml",
            etag='"' + hashlib.sha256(body).hexdigest()[:8] + '"',
            last_modified=None,
            moved_permanently=False,
            retry_after=None,
            bytes_read=len(body),
            requests=1,
            elapsed_ms=2,
            attempted_at=self.clock,
        )
        return replace(result, **self.overrides.get(feed_url, {}))


NOT_MODIFIED = {"outcome": "not_modified", "status": 304, "body": None, "bytes_read": 0}
FAILED = {"outcome": "failed", "reason": "http_5xx", "status": 503, "body": None}


class Ticker:
    """A wall clock that moves one minute per call, so each run gets its own fingerprint."""

    def __init__(self):
        self.now = T0

    def __call__(self):
        self.now += timedelta(minutes=1)
        return self.now


@pytest.fixture
def db(fresh_db, tmp_path):
    migrate(fresh_db)
    rows = [
        dump_row(1, podcastGuid="guid-1"),
        dump_row(2, podcastGuid="guid-2"),
        dump_row(3),
        dump_row(4),
    ]
    import_baseline(fresh_db, make_dump(tmp_path, rows), date(2024, 1, 1))
    return fresh_db


@pytest.fixture
def clock():
    return Ticker()


def refresh(conn, fetcher, clock, **kwargs):
    return refresh_podcasts(conn, fetcher, now=clock, monotonic=lambda: 0.0, **kwargs)


def count(conn, sql, *params):
    return conn.execute(sql, params).fetchone()[0]


def rss_rows(conn):
    return conn.execute(
        "select source_key, entity_id, provenance, last_seen_run, source_updated_at"
        " from source_records where source_id = 2 order by source_key"
    ).fetchall()


def dump_entity(conn, key):
    return count(
        conn, "select entity_id from source_records where source_id = 1 and source_key = %s", key
    )


def observations(conn, run_id=None):
    if run_id is None:
        return count(conn, "select count(*) from observations")
    return conn.execute(
        "select source_key, change_kind from observations where run_id = %s order by 1",
        (run_id,),
    ).fetchall()


def test_first_refresh_observes_every_feed_on_its_baseline_entity(db, clock):
    fetcher = FakeFetcher(bodies())

    report = refresh(db, fetcher, clock)

    assert report.status == "ok"
    rows = rss_rows(db)
    assert [r[0] for r in rows] == ["1", "2", "3", "4"]
    assert all(r[1] == dump_entity(db, r[0]) for r in rows)
    assert {r[2] for r in rows} == {"observed"}
    assert observations(db, report.run_id) == [(k, "new") for k in ("1", "2", "3", "4")]
    assert count(db, "select count(*) from feed_fetch_state") == 4
    assert count(db, "select count(*) from fetch_attempts where run_id = %s", report.run_id) == 4
    assert count(db, "select count(*) from contact_routes") == 2
    m = report.metrics
    assert (m.rows_in, m.rows_new, m.rows_changed, m.rows_unchanged) == (4, 4, 0, 0)
    assert (m.rows_not_modified, m.rows_failed, m.rows_skipped, m.rows_rejected) == (0, 0, 0, 0)
    assert (m.identifiers_added, m.candidates_created, m.http_requests) == (0, 0, 4)
    assert m.bytes_fetched == sum(len(b) for b in bodies().values())
    assert m.entities_created is None
    assert read_metrics(db, report.run_id) == m
    assert dict(report.outcomes) == {"fetched": 4}
    assert dict(report.activity) == {"active": 4}


def test_repeating_a_refresh_with_unchanged_sources_adds_no_observations(db, clock):
    first = refresh(db, FakeFetcher(bodies()), clock)
    before = observations(db)
    fetcher = FakeFetcher(bodies())

    second = refresh(db, fetcher, clock, same_as=first.run_id)

    assert observations(db) == before
    m = second.metrics
    assert (m.rows_new, m.rows_changed, m.rows_unchanged, m.identifiers_added) == (0, 0, 4, 0)
    assert {r[0] for r in db.execute("select checked_run from contact_routes")} == {second.run_id}
    assert [c[0] for c in fetcher.calls] == [url(k) for k in ("1", "2", "3", "4")]


def test_second_refresh_sends_stored_validators(db, clock):
    first = refresh(db, FakeFetcher(bodies()), clock)
    fetcher = FakeFetcher(bodies())

    refresh(db, fetcher, clock, same_as=first.run_id)

    assert all(etag is not None for _, etag, _ in fetcher.calls)


def test_not_modified_feeds_touch_routes_but_not_records(db, clock):
    first = refresh(db, FakeFetcher(bodies()), clock)
    before = rss_rows(db)
    overrides = {url(k): NOT_MODIFIED for k in ("1", "2", "3", "4")}

    third = refresh(db, FakeFetcher(bodies(), overrides), clock, same_as=first.run_id)

    assert third.metrics.rows_not_modified == 4
    assert third.metrics.rows_unchanged == 4
    assert observations(db, third.run_id) == []
    assert rss_rows(db) == before
    assert {r[0] for r in db.execute("select checked_run from contact_routes")} == {third.run_id}


def test_a_new_episode_is_observed_as_a_change(db, clock):
    first = refresh(db, FakeFetcher(bodies()), clock)

    second = refresh(
        db,
        FakeFetcher(bodies(**{"2": rss("Show 2", guid="guid-2", episode=NEWER_EPISODE)})),
        clock,
        same_as=first.run_id,
    )

    assert observations(db, second.run_id) == [("2", "changed")]
    updated = dict((r[0], r[4]) for r in rss_rows(db))
    assert updated["2"] == datetime(2026, 10, 23, 10, 0, tzinfo=UTC)


def test_a_failed_fetch_leaves_earlier_data_alone_and_makes_the_run_partial(db, clock):
    first = refresh(db, FakeFetcher(bodies()), clock)
    before = {r[0]: r for r in rss_rows(db)}

    second = refresh(db, FakeFetcher(bodies(), {url("1"): FAILED}), clock, same_as=first.run_id)

    assert second.status == "partial"
    assert second.metrics.rows_failed == 1
    assert {r[0]: r for r in rss_rows(db)}["1"] == before["1"]
    assert count(db, "select checked_run from contact_routes where route_type = 'website'") == (
        first.run_id
    )
    assert (
        count(db, "select consecutive_failures from feed_fetch_state where source_key = '1'") == 1
    )
    status = count(db, "select status from import_runs where run_id = %s", second.run_id)
    assert status == "partial"


def test_an_html_page_is_rejected_without_making_the_run_partial(db, clock):
    report = refresh(db, FakeFetcher(bodies(**{"4": HTML})), clock)

    assert report.status == "ok"
    assert report.metrics.rows_rejected == 1
    assert db.execute(
        "select source_key, reason from import_rejects where run_id = %s", (report.run_id,)
    ).fetchall() == [("4", "unsupported_format")]
    assert count(db, "select outcome from fetch_attempts where source_key = '4'") == "rejected"
    assert dict(report.outcomes) == {"fetched": 3, "rejected": 1}


def test_a_permanent_move_updates_the_fetch_url_and_attaches_the_new_address(db, clock):
    moved = {url("4"): {"final_url": "https://moved.example.com/4.xml", "moved_permanently": True}}

    report = refresh(db, FakeFetcher(bodies(), moved), clock)

    fetch_url = count(db, "select fetch_url from feed_fetch_state where source_key = '4'")
    assert fetch_url == "https://moved.example.com/4.xml"
    owner = count(
        db,
        "select entity_id from entity_identifiers where id_kind = 'feed_url'"
        " and id_value = 'moved.example.com/4.xml'",
    )
    assert owner == dump_entity(db, "4")
    attrs = count(db, "select attrs from source_records where source_id = 2 and source_key = '4'")
    assert attrs["moved_to"] == "https://moved.example.com/4.xml"
    assert report.metrics.identifiers_added == 1


def test_a_guid_owned_by_another_entity_becomes_a_candidate(db, clock):
    entities_before = count(db, "select count(*) from entities")

    report = refresh(db, FakeFetcher(bodies(**{"3": rss("Show 3", guid="guid-2")})), clock)

    assert report.metrics.candidates_created == 1
    assert db.execute(
        "select source_id, source_key, entity_id from candidate_matches where source_id = 2"
    ).fetchall() == [(2, "3", dump_entity(db, "2"))]
    assert count(db, "select entity_id from entity_identifiers where id_value = 'guid-2'") == (
        dump_entity(db, "2")
    )
    assert count(db, "select count(*) from entities") == entities_before


def test_a_repeated_conflict_is_not_counted_again(db, clock):
    feeds = bodies(**{"3": rss("Show 3", guid="guid-2")})
    first = refresh(db, FakeFetcher(feeds), clock)

    second = refresh(db, FakeFetcher(feeds), clock, same_as=first.run_id)

    assert second.metrics.candidates_created == 0


def test_refresh_rotates_through_the_baseline(db, clock):
    first_fetcher, second_fetcher = FakeFetcher(bodies()), FakeFetcher(bodies())

    refresh(db, first_fetcher, clock, limit=2)
    refresh(db, second_fetcher, clock, limit=2)

    assert [c[0] for c in first_fetcher.calls] == [url("1"), url("2")]
    assert [c[0] for c in second_fetcher.calls] == [url("3"), url("4")]


def test_a_refresh_left_running_by_a_crash_is_marked_failed(db, clock):
    stale = register_run(
        db,
        source_id=2,
        run_kind="refresh",
        fingerprint="stale",
        is_complete_snapshot=False,
        params={},
    ).run_id

    refresh(db, FakeFetcher(bodies()), clock)

    assert count(db, "select status from import_runs where run_id = %s", stale) == "failed"


def test_a_second_refresh_at_the_same_time_is_refused(db, clock, pg_url):
    runs_before = count(db, "select count(*) from import_runs")
    with psycopg.connect(pg_url, autocommit=True) as other:
        other.execute("select pg_advisory_lock(%s)", (REFRESH_LOCK_KEY,))

        with pytest.raises(RefreshBusy):
            refresh(db, FakeFetcher(bodies()), clock)

    assert count(db, "select count(*) from import_runs") == runs_before


def test_the_lock_is_released_after_a_run(db, clock, pg_url):
    refresh(db, FakeFetcher(bodies()), clock, limit=1)

    with psycopg.connect(pg_url, autocommit=True) as other:
        acquired = other.execute("select pg_try_advisory_lock(%s)", (REFRESH_LOCK_KEY,))
        assert acquired.fetchone()[0] is True


def test_same_as_must_name_a_refresh_run(db, clock):
    baseline_run = count(db, "select min(run_id) from import_runs")

    for run_id in (baseline_run, 999_999):
        with pytest.raises(RefreshInputError, match="refresh"):
            refresh(db, FakeFetcher(bodies()), clock, same_as=run_id)


def test_batches_keep_every_reject_and_metrics_accumulate(db, clock):
    feeds = bodies(**{"1": HTML, "4": HTML})

    report = refresh(db, FakeFetcher(feeds), clock, batch_size=2)

    assert db.execute(
        "select row_number, source_key from import_rejects order by 1"
    ).fetchall() == [
        (1, "1"),
        (4, "4"),
    ]
    assert (report.metrics.rows_rejected, report.metrics.rows_new) == (2, 2)
    assert read_metrics(db, report.run_id) == report.metrics


def test_a_feed_that_raises_is_recorded_as_failed_and_the_run_continues(db, clock):
    fetcher = FakeFetcher(bodies(), fail_on=url("3"))

    report = refresh(db, fetcher, clock, batch_size=2)

    assert report.status == "partial"
    assert [r[0] for r in rss_rows(db)] == ["1", "2", "4"]
    reason = count(db, "select reason from fetch_attempts where source_key = '3'")
    assert reason == "internal_error"


def test_a_write_failure_fails_the_run_and_keeps_committed_batches(db, clock, monkeypatch):
    from ddm.ingest import refresh as refresh_module

    real_write = refresh_module.write_batch
    calls = []

    def failing_write(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("database exploded")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(refresh_module, "write_batch", failing_write)

    with pytest.raises(RuntimeError, match="exploded"):
        refresh(db, FakeFetcher(bodies()), clock, batch_size=2)

    assert count(db, "select status from import_runs where source_id = 2") == "failed"
    assert [r[0] for r in rss_rows(db)] == ["1", "2"]


def test_a_dropped_connection_surfaces_the_original_error(db, clock, monkeypatch):
    from ddm.ingest import refresh as refresh_module

    def drop_connection(conn, *args, **kwargs):
        conn.close()
        raise RuntimeError("original failure")

    monkeypatch.setattr(refresh_module, "write_batch", drop_connection)

    with pytest.raises(RuntimeError, match="original failure"):
        refresh(db, FakeFetcher(bodies()), clock)


def test_a_non_autocommit_connection_is_refused(pg_url, clock):
    with psycopg.connect(pg_url) as conn:
        with pytest.raises(RefreshInputError, match="autocommit"):
            refresh(conn, FakeFetcher({}), clock)


def test_same_as_does_not_contact_feeds_that_are_backing_off(db, clock):
    refused = {
        url("2"): {
            "outcome": "failed",
            "reason": "refused",
            "status": 429,
            "body": None,
            "retry_after": T0 + timedelta(days=5),
        }
    }
    first = refresh(db, FakeFetcher(bodies(), refused), clock)
    fetcher = FakeFetcher(bodies())

    refresh(db, fetcher, clock, same_as=first.run_id)

    assert url("2") not in [c[0] for c in fetcher.calls]


def test_same_as_records_no_limit(db, clock):
    first = refresh(db, FakeFetcher(bodies()), clock)

    second = refresh(db, FakeFetcher(bodies()), clock, same_as=first.run_id)

    params = count(db, "select params from import_runs where run_id = %s", second.run_id)
    assert (params["limit"], params["same_as"]) == (None, first.run_id)


def test_refresh_never_creates_entities(db, clock):
    before = count(db, "select count(*) from entities")

    refresh(db, FakeFetcher(bodies()), clock)

    assert count(db, "select count(*) from entities") == before


def test_nothing_to_refresh_is_an_ok_run_with_zero_counts(fresh_db, clock):
    migrate(fresh_db)

    report = refresh(fresh_db, FakeFetcher({}), clock)

    assert report.status == "ok"
    assert (report.metrics.rows_in, report.metrics.rows_new) == (0, 0)


@pytest.mark.parametrize("limit", [0, 5001])
def test_limit_must_be_within_bounds(db, clock, limit):
    with pytest.raises(RefreshInputError, match="limit"):
        refresh(db, FakeFetcher(bodies()), clock, limit=limit)
