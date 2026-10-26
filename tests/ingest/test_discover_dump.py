from datetime import date

import pytest

from ddm.db.migrate import migrate
from ddm.ingest.baseline import import_baseline
from ddm.ingest.discover_dump import DiscoveryInputError, discover_dump
from ddm.ingest.discovery import discover_batch
from tests.conftest import dump_row, make_dump
from tests.ingest.test_discovery import baseline_rows, candidate_rows

pytestmark = pytest.mark.integration


def fixed_clock():
    return 100.0


@pytest.fixture
def db(fresh_db):
    migrate(fresh_db)
    return fresh_db


@pytest.fixture
def dump(tmp_path):
    return make_dump(tmp_path, baseline_rows() + candidate_rows())


def count(conn, table):
    return conn.execute(f"select count(*) from {table}").fetchone()[0]  # noqa: S608


def test_discovery_classifies_the_window_and_reports_counts(db, dump):
    import_baseline(db, dump)

    report = discover_dump(db, dump, clock=fixed_clock)

    m = report.metrics
    assert (report.status, report.skipped) == ("ok", False)
    assert (m.rows_in, m.rows_rejected, m.rows_new) == (9, 0, 9)
    assert (
        m.discovered_new,
        m.discovered_rename_or_move,
        m.discovered_duplicate,
        m.discovered_uncertain,
    ) == (2, 2, 1, 4)  # 108 and 109 share only a GUID under a new title: both go to review
    assert m.db_size_bytes > 0
    status, kind, snapshot = db.execute(
        "select status, run_kind, is_complete_snapshot from import_runs where run_id = %s",
        (report.run_id,),
    ).fetchone()
    assert (status, kind, snapshot) == ("ok", "discovery", False)


def test_the_same_dump_twice_is_registered_once_and_writes_nothing(db, dump):
    import_baseline(db, dump)
    first = discover_dump(db, dump, clock=fixed_clock)
    before = {t: count(db, t) for t in ("discoveries", "observations", "entities")}

    second = discover_dump(db, dump, clock=fixed_clock)

    assert (second.run_id, second.skipped) == (first.run_id, True)
    assert {t: count(db, t) for t in before} == before


def test_discovery_needs_the_baseline_first(db, dump):
    with pytest.raises(DiscoveryInputError, match="baseline"):
        discover_dump(db, dump, clock=fixed_clock)


def test_an_empty_window_finishes_ok_with_zero_counts(db, tmp_path):
    path = make_dump(tmp_path, baseline_rows())
    import_baseline(db, path)

    report = discover_dump(db, path, clock=fixed_clock)

    assert report.status == "ok"
    assert (report.metrics.rows_in, report.metrics.discovered_new) == (0, 0)


def test_a_later_window_start_leaves_earlier_candidates_out(db, dump):
    import_baseline(db, dump)

    report = discover_dump(db, dump, created_from=date(2025, 1, 1), clock=fixed_clock)

    assert report.metrics.rows_in == 0


def test_rejected_candidates_are_kept_and_counted(db, tmp_path):
    rows = baseline_rows() + [dump_row(150, url="not a url", createdOn=1710000000)]
    path = make_dump(tmp_path, rows)
    import_baseline(db, path)

    report = discover_dump(db, path, clock=fixed_clock)

    assert report.metrics.rows_rejected == 1
    reason = db.execute(
        "select reason from import_rejects where run_id = %s", (report.run_id,)
    ).fetchone()
    assert reason == ("invalid_feed_url",)


def test_a_failed_discovery_rolls_back_and_the_rerun_reuses_the_run(db, dump):
    import_baseline(db, dump)

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        discover_dump(db, dump, clock=fixed_clock, batch=broken)
    assert count(db, "discoveries") == 0

    report = discover_dump(db, dump, clock=fixed_clock)

    assert report.status == "ok"
    assert count(db, "discoveries") == 9


def test_identifiers_shared_across_baseline_and_window_become_ambiguous(db, tmp_path):
    shared = {"podcastGuid": "host-default"}
    rows = [dump_row(1, **shared), dump_row(2, **shared)] + [
        dump_row(k, createdOn=1710000000, **shared) for k in (101, 102)
    ]
    path = make_dump(tmp_path, rows)
    import_baseline(db, path)

    report = discover_dump(db, path, clock=fixed_clock)

    assert report.metrics.ambiguous_identifiers == 1
    entities = db.execute(
        "select count(distinct entity_id) from source_records where source_key in ('101', '102')"
    ).fetchone()[0]
    assert entities == 2


def all_classes(conn):
    rows = conn.execute("select source_key, classification, method from discoveries").fetchall()
    return {key: (kind, method) for key, kind, method in rows}


def test_classification_does_not_depend_on_the_batch_size(db, dump, fresh_db, tmp_path):
    import_baseline(db, dump)
    whole = discover_dump(db, dump, clock=fixed_clock)
    expected = all_classes(db)

    migrate_again = fresh_db
    migrate_again.execute("DROP SCHEMA public CASCADE")
    migrate_again.execute("CREATE SCHEMA public")
    migrate(migrate_again)
    import_baseline(migrate_again, dump)
    batched = discover_dump(migrate_again, dump, clock=fixed_clock, batch_size=2)

    assert all_classes(migrate_again) == expected
    assert batched.metrics.discovered_new == whole.metrics.discovered_new
    assert batched.metrics.rows_in == whole.metrics.rows_in == 9


def test_a_crash_after_some_batches_resumes_and_counts_every_record_once(db, dump):
    import_baseline(db, dump)
    calls = []

    def fail_on_second_batch(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return discover_batch(*args, **kwargs)

    with pytest.raises(RuntimeError, match="boom"):
        discover_dump(db, dump, clock=fixed_clock, batch=fail_on_second_batch, batch_size=4)
    assert count(db, "discoveries") == 4

    report = discover_dump(db, dump, clock=fixed_clock, batch_size=4)

    m = report.metrics
    assert (report.status, count(db, "discoveries")) == ("ok", 9)
    assert (m.rows_in, m.rows_new) == (9, 9)
    assert (
        m.discovered_new
        + m.discovered_rename_or_move
        + m.discovered_duplicate
        + m.discovered_uncertain
    ) == 9
