from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest

from ddm.db.migrate import migrate
from ddm.ingest.baseline import import_baseline
from ddm.ingest.contacts import touch_contact_routes, upsert_contact_routes
from ddm.ingest.fetch_log import (
    Attempt,
    RefreshTarget,
    fail_stale_runs,
    select_targets,
    targets_of_run,
    update_fetch_state,
    write_attempts,
)
from ddm.ingest.runs import finish_run, register_run
from ddm.sources.podcast_rss import ContactRoute
from ddm.sources.rss_fetch import FetchResult
from tests.conftest import dump_row, make_dump

pytestmark = pytest.mark.integration

DUMP, RSS = 1, 2
T0 = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(fresh_db, tmp_path):
    migrate(fresh_db)
    rows = [dump_row(k) for k in (1, 2, 3, 10)]
    import_baseline(fresh_db, make_dump(tmp_path, rows), date(2024, 1, 1))
    return fresh_db


_runs = iter(range(1, 10_000))


def new_run(conn, status=None):
    handle = register_run(
        conn,
        source_id=RSS,
        run_kind="refresh",
        fingerprint=f"fp-{next(_runs)}",
        is_complete_snapshot=False,
        params={},
    )
    if status:
        finish_run(conn, handle.run_id, status)
    return handle.run_id


def entity_of(conn, key):
    return conn.execute(
        "select entity_id from source_records where source_id = 1 and source_key = %s", (key,)
    ).fetchone()[0]


def result(outcome="fetched", reason=None, **overrides):
    url = overrides.pop("url", "https://feeds.example.com/1.xml")
    values = {
        "requested_url": url,
        "final_url": url,
        "outcome": outcome,
        "reason": reason,
        "status": 200,
        "body": None,
        "content_type": None,
        "etag": None,
        "last_modified": None,
        "moved_permanently": False,
        "retry_after": None,
        "bytes_read": 10,
        "requests": 1,
        "elapsed_ms": 3,
        "attempted_at": T0,
    }
    return FetchResult(**{**values, **overrides})


def attempt(key, outcome="fetched", reason=None, **overrides):
    if outcome == "rejected":
        return Attempt(source_key=key, outcome=outcome, reason=reason, fetch=result(**overrides))
    fetch = result(outcome, reason, **overrides)
    return Attempt(source_key=key, outcome=outcome, reason=reason, fetch=fetch)


def write_state(conn, run_id, attempts):
    with conn.transaction():
        update_fetch_state(conn, run_id, attempts)


def state(conn, key):
    return conn.execute(
        """select fetch_url, etag, last_modified, last_outcome, last_ok_at,
                  consecutive_failures, retry_after
           from feed_fetch_state where source_key = %s""",
        (key,),
    ).fetchone()


def route(value="owner@example.com", route_type="feed_owner_email"):
    return ContactRoute(
        route_type=route_type,
        value=value,
        syntax_valid=True,
        affiliation_status="confirmed",
        affiliation_basis="basis",
        purpose="unknown",
        purpose_basis="purpose basis",
    )


def upsert_routes(conn, run_id, rows):
    with conn.transaction():
        return upsert_contact_routes(conn, rows, source_id=RSS, run_id=run_id)


def touch(conn, run_id, keys, at=T0):
    with conn.transaction():
        return touch_contact_routes(conn, [(k, at) for k in keys], source_id=RSS, run_id=run_id)


def routes(conn):
    return conn.execute(
        "select value, source_key, first_seen_run, checked_run from contact_routes order by value"
    ).fetchall()


def baseline_run_id(conn):
    return conn.execute("select min(run_id) from import_runs").fetchone()[0]


def test_select_targets_returns_never_fetched_feeds_in_numeric_order(db):
    targets = select_targets(db, dump_id=DUMP, limit=10, now=T0)

    assert [t.source_key for t in targets] == ["1", "2", "3", "10"]
    assert targets[0] == RefreshTarget(
        source_key="1",
        entity_id=entity_of(db, "1"),
        url="https://feeds.example.com/1.xml",
        etag=None,
        last_modified=None,
    )


def test_select_targets_applies_the_limit(db):
    assert len(select_targets(db, dump_id=DUMP, limit=2, now=T0)) == 2


def test_select_targets_rotates_least_recently_attempted_first(db):
    run_id = new_run(db)
    write_state(db, run_id, [attempt("1"), attempt("2", attempted_at=T0 - timedelta(hours=1))])

    targets = select_targets(db, dump_id=DUMP, limit=4, now=T0)

    assert [t.source_key for t in targets] == ["3", "10", "2", "1"]


def test_select_targets_uses_stored_url_and_validators(db):
    run_id = new_run(db)
    moved = attempt("1", final_url="https://new.example.com/1", moved_permanently=True, etag='"e"')
    write_state(db, run_id, [moved])

    targets = select_targets(db, dump_id=DUMP, limit=4, now=T0)

    target = next(t for t in targets if t.source_key == "1")
    assert (target.url, target.etag) == ("https://new.example.com/1", '"e"')


def test_select_targets_skips_feeds_backing_off_deleted_or_unresolved(db):
    run_id = new_run(db)
    backing_off = attempt("1", "failed", "refused", retry_after=T0 + timedelta(days=1))
    write_state(db, run_id, [backing_off])
    db.execute(
        "update source_records set deleted_at = now(), deleted_run = %s where source_key = '2'",
        (run_id,),
    )
    db.execute("update source_records set entity_id = null where source_key = '3'")

    keys = [t.source_key for t in select_targets(db, dump_id=DUMP, limit=10, now=T0)]

    assert keys == ["10"]


def test_select_targets_includes_feeds_whose_back_off_has_passed(db):
    run_id = new_run(db)
    write_state(db, run_id, [attempt("1", "failed", "refused", retry_after=T0)])

    keys = [t.source_key for t in select_targets(db, dump_id=DUMP, limit=10, now=T0)]

    assert "1" in keys


def test_fetched_state_stores_validators_and_resets_failures(db):
    first, second = new_run(db), new_run(db)
    write_state(db, first, [attempt("1", "failed", "http_5xx")])

    write_state(db, second, [attempt("1", etag='"e"', last_modified="lm")])

    _, etag, lm, outcome, ok_at, failures, _ = state(db, "1")
    assert (etag, lm, outcome, ok_at, failures) == ('"e"', "lm", "fetched", T0, 0)


def test_failed_state_keeps_validators_and_last_ok_and_counts_failures(db):
    first, second, third = new_run(db), new_run(db), new_run(db)
    write_state(db, first, [attempt("1", etag='"e"', last_modified="lm")])
    later = T0 + timedelta(days=1)

    write_state(db, second, [attempt("1", "failed", "http_5xx", attempted_at=later)])
    write_state(db, third, [attempt("1", "failed", "network_error", attempted_at=later)])

    _, etag, lm, outcome, ok_at, failures, _ = state(db, "1")
    assert (etag, lm, outcome, ok_at, failures) == ('"e"', "lm", "failed", T0, 2)


def test_rejected_state_counts_as_answered_but_stores_no_validators(db):
    run_id = new_run(db)

    write_state(db, run_id, [attempt("1", "rejected", "malformed_xml", etag='"e"')])

    _, etag, _, outcome, ok_at, failures, _ = state(db, "1")
    assert (etag, outcome, ok_at, failures) == (None, "rejected", T0, 0)


def test_skipped_state_leaves_the_failure_count_alone(db):
    first, second = new_run(db), new_run(db)
    write_state(db, first, [attempt("1", "failed", "http_5xx")])

    write_state(db, second, [attempt("1", "skipped", "robots_disallowed")])

    assert state(db, "1")[5] == 1


def test_retry_after_is_cleared_by_a_later_success(db):
    first, second = new_run(db), new_run(db)
    write_state(db, first, [attempt("1", "failed", "refused", retry_after=T0)])

    write_state(db, second, [attempt("1")])

    assert state(db, "1")[6] is None


def test_permanent_redirect_updates_the_fetch_url_and_temporary_does_not(db):
    run_id = new_run(db)
    permanent = attempt("1", final_url="https://new.example.com/1", moved_permanently=True)
    temporary = attempt(
        "2", url="https://feeds.example.com/2.xml", final_url="https://cdn.example.com/2"
    )

    write_state(db, run_id, [permanent, temporary])

    assert state(db, "1")[0] == "https://new.example.com/1"
    assert state(db, "2")[0] == "https://feeds.example.com/2.xml"


def test_write_attempts_round_trips(db):
    run_id = new_run(db)

    with db.transaction():
        written = write_attempts(
            db, run_id, [attempt("1"), attempt("2", "failed", "http_404", status=404)]
        )

    assert written == 2
    assert db.execute(
        "select source_key, outcome, reason, http_status, bytes from fetch_attempts"
        " order by source_key"
    ).fetchall() == [("1", "fetched", None, 200, 10), ("2", "failed", "http_404", 404, 10)]


def test_failed_attempt_without_reason_is_refused_by_the_database(db):
    run_id = new_run(db)

    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction():
            write_attempts(db, run_id, [attempt("1", "failed", None)])


def test_targets_of_run_repeats_the_feeds_of_an_earlier_refresh_in_order(db):
    run_id = new_run(db)
    later = T0 + timedelta(seconds=1)
    with db.transaction():
        write_attempts(db, run_id, [attempt("10"), attempt("2", attempted_at=later)])

    targets = targets_of_run(db, run_id, dump_id=DUMP, rss_id=RSS, now=T0)

    assert [t.source_key for t in targets] == ["10", "2"]


def test_targets_of_run_leaves_out_feeds_that_are_backing_off(db):
    run_id = new_run(db)
    with db.transaction():
        write_attempts(db, run_id, [attempt("1"), attempt("2")])
    write_state(db, new_run(db), [attempt("1", "failed", "refused", retry_after=T0 + timedelta(1))])

    targets = targets_of_run(db, run_id, dump_id=DUMP, rss_id=RSS, now=T0)

    assert [t.source_key for t in targets] == ["2"]


def test_targets_of_run_refuses_runs_that_are_not_refreshes_with_attempts(db):
    empty_refresh = new_run(db)

    for run_id in (baseline_run_id(db), empty_refresh, 999_999):
        with pytest.raises(ValueError, match="refresh"):
            targets_of_run(db, run_id, dump_id=DUMP, rss_id=RSS, now=T0)


def test_fail_stale_runs_marks_only_running_rss_refreshes(db):
    stale = new_run(db)
    finished = new_run(db, status="ok")
    baseline = baseline_run_id(db)
    db.execute(
        "update import_runs set status = 'running', finished_at = null where run_id = %s",
        (baseline,),
    )

    marked = fail_stale_runs(db, RSS)

    statuses = dict(db.execute("select run_id, status from import_runs").fetchall())
    assert marked == 1
    assert (statuses[stale], statuses[finished], statuses[baseline]) == ("failed", "ok", "running")


def test_first_upsert_inserts_routes_seen_and_checked_in_this_run(db):
    run_id = new_run(db)

    upsert_routes(db, run_id, [(entity_of(db, "1"), "1", route(), T0)])

    assert routes(db) == [("owner@example.com", "1", run_id, run_id)]
    assert db.execute(
        "select deliverability_status, provenance, source_id from contact_routes"
    ).fetchone() == ("unchecked", "observed", RSS)


def test_upserting_again_in_the_same_run_changes_nothing(db):
    run_id = new_run(db)
    rows = [(entity_of(db, "1"), "1", route(), T0)]
    upsert_routes(db, run_id, rows)

    upsert_routes(db, run_id, rows)

    assert routes(db) == [("owner@example.com", "1", run_id, run_id)]


def test_a_later_run_advances_checked_run_but_not_first_seen(db):
    first, second = new_run(db), new_run(db)
    rows = [(entity_of(db, "1"), "1", route(), T0)]
    upsert_routes(db, first, rows)

    upsert_routes(db, second, rows)

    assert routes(db) == [("owner@example.com", "1", first, second)]


def test_two_feeds_of_one_entity_publishing_one_address_are_one_route(db):
    run_id = new_run(db)
    entity = entity_of(db, "1")

    upsert_routes(db, run_id, [(entity, "1", route(), T0), (entity, "2", route(), T0)])

    assert len(routes(db)) == 1


def fetched_with_routes(conn, run_id, rows):
    """What a refresh batch does for a parsed feed: routes, then fetch state."""
    upsert_routes(conn, run_id, rows)
    write_state(conn, run_id, [attempt("1")])


def test_touch_refreshes_routes_still_in_the_feed_but_not_vanished_ones(db):
    run1, run2, run3 = new_run(db), new_run(db), new_run(db)
    entity = entity_of(db, "1")
    both = [(entity, "1", route("a@example.com"), T0), (entity, "1", route("b@example.com"), T0)]
    fetched_with_routes(db, run1, both)
    fetched_with_routes(db, run2, [(entity, "1", route("a@example.com"), T0)])

    touched = touch(db, run3, ["1"], at=T0 + timedelta(days=2))

    assert touched == 1
    assert routes(db) == [("a@example.com", "1", run1, run3), ("b@example.com", "1", run1, run1)]
    checked_at = db.execute(
        "select checked_at from contact_routes where value = 'a@example.com'"
    ).fetchone()[0]
    assert checked_at == T0 + timedelta(days=2)


def test_touch_does_not_revive_routes_when_the_latest_body_had_none(db):
    run1, run2, run3 = new_run(db), new_run(db), new_run(db)
    entity = entity_of(db, "1")
    fetched_with_routes(db, run1, [(entity, "1", route("a@example.com"), T0)])
    fetched_with_routes(db, run2, [])

    touched = touch(db, run3, ["1"])

    assert touched == 0
    assert routes(db) == [("a@example.com", "1", run1, run1)]


def test_touch_does_not_count_a_rejected_body_as_the_latest_fetch(db):
    run1, run2, run3 = new_run(db), new_run(db), new_run(db)
    entity = entity_of(db, "1")
    fetched_with_routes(db, run1, [(entity, "1", route("a@example.com"), T0)])
    write_state(db, run2, [attempt("1", "rejected", "malformed_xml")])

    assert touch(db, run3, ["1"]) == 1


def test_a_permanent_redirect_that_then_failed_keeps_the_old_fetch_url(db):
    run_id = new_run(db)
    moved_then_404 = attempt(
        "1",
        "failed",
        "http_404",
        status=404,
        final_url="https://dead.example.com/1",
        moved_permanently=True,
    )

    write_state(db, run_id, [moved_then_404])

    assert state(db, "1")[0] == "https://feeds.example.com/1.xml"


def test_select_targets_skips_records_without_a_feed_url(db):
    db.execute("update source_records set attrs = attrs - 'feed_url' where source_key = '1'")

    keys = [t.source_key for t in select_targets(db, dump_id=DUMP, limit=10, now=T0)]

    assert "1" not in keys


def test_touch_without_routes_does_nothing(db):
    run_id = new_run(db)

    assert touch(db, run_id, ["1"]) == 0
    assert touch(db, run_id, []) == 0
