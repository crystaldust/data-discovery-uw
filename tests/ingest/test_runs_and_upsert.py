from datetime import UTC, datetime

import pytest

from ddm.db.migrate import migrate
from ddm.ingest.records import Reject, make_record
from ddm.ingest.runs import (
    RunMetrics,
    add_metrics,
    append_rejects,
    finish_run,
    read_metrics,
    register_run,
    source_id_of,
    write_metrics,
    write_rejects,
)
from ddm.ingest.upsert import upsert_records

pytestmark = pytest.mark.integration

SOURCE_ID = 1


@pytest.fixture
def db(fresh_db):
    migrate(fresh_db)
    return fresh_db


def register(conn, fingerprint="fp-1"):
    return register_run(
        conn,
        source_id=SOURCE_ID,
        run_kind="historical_backfill",
        fingerprint=fingerprint,
        is_complete_snapshot=True,
        params={"created_before": "2024-01-01"},
    )


def record(key, name="Show"):
    return make_record(
        source_key=key,
        display_name=name,
        attrs={"feed_url": f"https://a.com/{key}"},
        source_updated_at=None,
        identifiers=(("podcastindex_id", key),),
    )


def upsert(conn, run_id, records):
    with conn.transaction():
        return upsert_records(
            conn, source_id=SOURCE_ID, run_id=run_id, provenance="historical", records=records
        )


def count(conn, table):
    return conn.execute(f"select count(*) from {table}").fetchone()[0]  # noqa: S608


def test_register_run_returns_a_new_running_run(db):
    handle = register(db)

    assert handle.status == "running"
    assert handle.resumed is False


def test_registering_the_same_input_again_resumes_the_running_run(db):
    first = register(db)

    second = register(db)

    assert second.run_id == first.run_id
    assert second.resumed is True
    assert count(db, "import_runs") == 1


def test_registering_a_finished_input_reports_it_as_finished(db):
    first = register(db)
    finish_run(db, first.run_id, "ok")

    again = register(db)

    assert (again.run_id, again.status) == (first.run_id, "ok")


def test_registering_a_failed_input_reopens_it(db):
    first = register(db)
    finish_run(db, first.run_id, "failed")

    again = register(db)

    assert (again.run_id, again.status, again.resumed) == (first.run_id, "running", True)


def test_finish_run_rejects_unknown_status(db):
    handle = register(db)

    with pytest.raises(ValueError, match="unknown run status"):
        finish_run(db, handle.run_id, "done")


def test_metrics_round_trip_and_unmeasured_values_stay_null(db):
    handle = register(db)
    metrics = RunMetrics(rows_in=3, rows_new=3)

    with db.transaction():
        write_metrics(db, handle.run_id, metrics)

    assert read_metrics(db, handle.run_id) == metrics
    assert read_metrics(db, handle.run_id).http_requests is None


def test_read_metrics_is_none_before_any_data_is_committed(db):
    handle = register(db)

    assert read_metrics(db, handle.run_id) is None


def test_upsert_can_run_twice_in_one_transaction(db):
    run = register(db).run_id

    with db.transaction():
        upsert_records(
            db, source_id=SOURCE_ID, run_id=run, provenance="historical", records=[record("1")]
        )
        second = upsert_records(
            db, source_id=SOURCE_ID, run_id=run, provenance="historical", records=[record("2")]
        )

    assert second.new_keys == ("2",)
    assert count(db, "source_records") == 2


def test_observation_payload_omits_unknown_values_and_uses_utc(db):
    run = register(db).run_id
    known = make_record(
        source_key="1",
        display_name=None,
        attrs={"feed_url": "https://a.com/1"},
        source_updated_at=datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC),
        identifiers=(),
    )
    db.execute("SET TIME ZONE 'Asia/Shanghai'")

    upsert(db, run, [known])

    payload = db.execute("select payload from observations").fetchone()[0]
    assert payload == {
        "attrs": {"feed_url": "https://a.com/1"},
        "source_updated_at": "2023-11-14T22:13:20+00:00",
    }


def test_write_rejects_replaces_rows_from_an_interrupted_attempt(db):
    handle = register(db)
    rejects = [Reject(row_number=1, source_key="7", reason="invalid_feed_url", raw={"url": ""})]

    with db.transaction():
        write_rejects(db, handle.run_id, rejects)
    with db.transaction():
        written = write_rejects(db, handle.run_id, rejects)

    assert written == 1
    assert db.execute("select source_key, reason, raw from import_rejects").fetchall() == [
        ("7", "invalid_feed_url", {"url": ""})
    ]


def test_first_upsert_marks_every_record_new_and_observes_it(db):
    run = register(db).run_id

    result = upsert(db, run, [record("1"), record("2")])

    assert (result.new_keys, result.changed_keys, result.unchanged) == (("1", "2"), (), 0)
    assert count(db, "observations") == 2
    assert db.execute("select distinct provenance from source_records").fetchall() == [
        ("historical",)
    ]


def test_unchanged_records_write_no_observations_and_only_refresh_last_seen(db):
    first = register(db, "fp-1").run_id
    upsert(db, first, [record("1"), record("2")])
    second = register(db, "fp-2").run_id

    result = upsert(db, second, [record("1"), record("2")])

    assert (result.new_keys, result.changed_keys, result.unchanged) == ((), (), 2)
    assert count(db, "observations") == 2
    assert db.execute(
        "select distinct first_seen_run, last_seen_run, last_changed_run from source_records"
    ).fetchall() == [(first, second, first)]


def test_a_changed_title_is_observed_as_changed(db):
    first = register(db, "fp-1").run_id
    upsert(db, first, [record("1"), record("2")])
    second = register(db, "fp-2").run_id

    result = upsert(db, second, [record("1", name="Renamed"), record("2")])

    assert (result.new_keys, result.changed_keys, result.unchanged) == ((), ("1",), 1)
    row = db.execute(
        "select change_kind, payload->>'display_name' from observations where run_id = %s",
        (second,),
    ).fetchone()
    assert row == ("changed", "Renamed")


def test_upsert_requires_an_open_transaction(db):
    run = register(db).run_id

    with pytest.raises(RuntimeError, match="transaction"):
        upsert_records(
            db, source_id=SOURCE_ID, run_id=run, provenance="historical", records=[record("1")]
        )


def test_upsert_rejects_duplicate_keys_in_one_batch(db):
    run = register(db).run_id

    with pytest.raises(ValueError, match="duplicate source_key"):
        upsert(db, run, [record("1"), record("1")])


def test_upsert_of_nothing_writes_nothing(db):
    run = register(db).run_id

    result = upsert(db, run, [])

    assert (result.new_keys, result.changed_keys, result.unchanged) == ((), (), 0)
    assert count(db, "source_records") == 0


def test_add_metrics_sums_measured_values_and_keeps_unmeasured_ones_null():
    first = RunMetrics(rows_in=1)
    second = RunMetrics(rows_in=2, rows_failed=1)

    total = add_metrics(first, second)

    assert total == RunMetrics(rows_in=3, rows_failed=1)
    assert total.http_requests is None
    assert first == RunMetrics(rows_in=1)


def test_new_fetch_outcome_metrics_round_trip(db):
    handle = register(db)
    metrics = RunMetrics(rows_in=5, rows_not_modified=2, rows_failed=1, rows_skipped=1)

    with db.transaction():
        write_metrics(db, handle.run_id, metrics)

    assert read_metrics(db, handle.run_id) == metrics


def test_append_rejects_keeps_rows_from_earlier_batches(db):
    handle = register(db)
    first = [Reject(row_number=1, source_key="1", reason="malformed_xml", raw={})]
    second = [Reject(row_number=7, source_key="7", reason="unsupported_format", raw={})]

    with db.transaction():
        append_rejects(db, handle.run_id, first)
    with db.transaction():
        written = append_rejects(db, handle.run_id, second)

    assert written == 1
    assert db.execute(
        "select row_number, reason from import_rejects order by row_number"
    ).fetchall() == [(1, "malformed_xml"), (7, "unsupported_format")]


def test_source_id_of_finds_registered_sources(db):
    assert (source_id_of(db, "podcastindex_dump"), source_id_of(db, "podcast_rss")) == (1, 2)


def test_source_id_of_fails_for_an_unregistered_source(db):
    with pytest.raises(RuntimeError, match="not registered"):
        source_id_of(db, "nope")
