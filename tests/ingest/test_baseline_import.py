from datetime import date

import pytest

from ddm.db.migrate import migrate
from ddm.identifiers import entity_id_for
from ddm.ingest.baseline import import_baseline
from tests.conftest import dump_row, make_dump

pytestmark = pytest.mark.integration

TABLES = (
    "import_runs",
    "run_metrics",
    "import_rejects",
    "source_records",
    "observations",
    "entities",
    "entity_identifiers",
    "candidate_matches",
)


MOVED_FROM_3 = "http://feeds.example.com/3.xml"
URL_OF_1 = "https://feeds.example.com/1.xml"
SHARED_GUID = "c9c7bad3-4712-514e-9ebd-d1e208fa1b76"


def fixed_clock():
    return 100.0


def baseline_rows():
    return [
        dump_row(1, itunesId=900, podcastGuid="guid-a"),
        dump_row(2, itunesId=900),  # same Apple ID as 1 -> merged
        dump_row(3, podcastGuid="guid-c"),
        dump_row(4, podcastGuid="guid-a"),  # guid of 1 -> merged
        dump_row(5, originalUrl=URL_OF_1, podcastGuid="guid-c"),  # hits 1 and 3 -> conflict
        dump_row(6, title="", originalUrl=MOVED_FROM_3),  # migrated from 3 -> merged
        dump_row(7, url=""),  # rejected
        dump_row(8, language="fr"),  # outside the slice
        dump_row(9, itunesId=900, podcastGuid="guid-z"),  # Apple ID of 1, other guid -> conflict
        # One guid on four feeds is a publisher default, not an identity -> never merged.
        *(dump_row(k, podcastGuid=SHARED_GUID) for k in (10, 11, 12, 13)),
    ]


@pytest.fixture
def db(fresh_db):
    migrate(fresh_db)
    return fresh_db


def counts(conn):
    return {
        table: conn.execute(f"select count(*) from {table}").fetchone()[0]  # noqa: S608
        for table in TABLES
    }


def run_import(conn, path, **kwargs):
    return import_baseline(conn, path, date(2024, 1, 1), clock=fixed_clock, **kwargs)


def entity_of(conn, key):
    return conn.execute(
        "select entity_id from source_records where source_key = %s", (key,)
    ).fetchone()[0]


def test_first_import_reports_counts_and_marks_everything_historical(db, tmp_path):
    path = make_dump(tmp_path, baseline_rows())

    report = run_import(db, path)

    metrics = report.metrics
    assert (report.status, report.skipped) == ("ok", False)
    assert (metrics.rows_in, metrics.rows_rejected, metrics.rows_new) == (12, 1, 11)
    assert (metrics.rows_changed, metrics.rows_unchanged) == (0, 0)
    assert metrics.entities_created == 8
    assert metrics.strong_id_merges == 3
    assert metrics.candidates_created == 3
    assert metrics.ambiguous_identifiers == 1
    assert metrics.duration_ms == 0
    assert metrics.db_size_bytes > 0
    assert db.execute("select distinct provenance from source_records").fetchall() == [
        ("historical",)
    ]
    assert db.execute("select distinct first_seen_kind from entities").fetchall() == [
        ("historical",)
    ]
    assert counts(db)["observations"] == 11


def test_strong_identifier_merges_and_conflicts(db, tmp_path):
    run_import(db, make_dump(tmp_path, baseline_rows()))

    first = entity_id_for("podcastindex_dump", "1")
    third = entity_id_for("podcastindex_dump", "3")
    assert entity_of(db, "1") == entity_of(db, "2") == entity_of(db, "4") == first
    assert entity_of(db, "3") == entity_of(db, "6") == third
    assert entity_of(db, "5") == entity_id_for("podcastindex_dump", "5")
    assert entity_of(db, "9") == entity_id_for("podcastindex_dump", "9")
    candidates = db.execute(
        "select source_key, entity_id, method from candidate_matches"
    ).fetchall()
    assert set(candidates) == {
        ("5", first, "strong_id_conflict_v1"),
        ("5", third, "strong_id_conflict_v1"),
        ("9", first, "strong_id_conflict_v1"),
    }


def test_identifiers_shared_by_too_many_feeds_are_recorded_and_not_merged(db, tmp_path):
    run_import(db, make_dump(tmp_path, baseline_rows()))

    shared_entities = {entity_of(db, str(k)) for k in (10, 11, 12, 13)}
    ambiguous = db.execute(
        "select id_kind, id_value, record_count from ambiguous_identifiers"
    ).fetchall()
    attached = db.execute(
        "select count(*) from entity_identifiers where id_value = %s", (SHARED_GUID,)
    ).fetchone()[0]

    assert len(shared_entities) == 4
    assert ambiguous == [("podcast_guid", SHARED_GUID, 4)]
    assert attached == 0


def test_later_import_checks_new_records_against_existing_entities(db, tmp_path):
    rows = baseline_rows()
    run_import(db, make_dump(tmp_path, rows))
    later_dir = tmp_path / "later"
    later_dir.mkdir()
    rows.append(dump_row(20, originalUrl=URL_OF_1, podcastGuid="guid-new"))
    rows.append(dump_row(21, podcastGuid=SHARED_GUID))

    report = run_import(db, make_dump(later_dir, rows))

    assert report.metrics.rows_new == 2
    assert entity_of(db, "20") == entity_id_for("podcastindex_dump", "20")
    assert entity_of(db, "21") == entity_id_for("podcastindex_dump", "21")
    assert db.execute(
        "select entity_id from candidate_matches where source_key = '20'"
    ).fetchall() == [(entity_id_for("podcastindex_dump", "1"),)]


def test_reimporting_the_same_input_writes_nothing(db, tmp_path):
    path = make_dump(tmp_path, baseline_rows())
    first = run_import(db, path)
    before = counts(db)

    second = run_import(db, path)

    assert (second.run_id, second.status, second.skipped) == (first.run_id, "ok", True)
    assert second.metrics == first.metrics
    assert counts(db) == before


def test_changed_input_records_only_the_change(db, tmp_path):
    rows = baseline_rows()
    run_import(db, make_dump(tmp_path, rows))
    entity_before = entity_of(db, "6")
    changed_dir = tmp_path / "changed"
    changed_dir.mkdir()
    rows[5] = dump_row(6, title="Now Titled", originalUrl=MOVED_FROM_3)

    report = run_import(db, make_dump(changed_dir, rows))

    assert report.skipped is False
    assert (report.metrics.rows_new, report.metrics.rows_changed) == (0, 1)
    assert report.metrics.rows_unchanged == 10
    assert report.metrics.entities_created == 0
    assert entity_of(db, "6") == entity_before
    assert counts(db)["entities"] == 8


def test_rejected_rows_are_kept_with_their_raw_values(db, tmp_path):
    run_import(db, make_dump(tmp_path, baseline_rows()))

    row = db.execute("select source_key, reason, raw->>'url' from import_rejects").fetchone()

    assert row == ("7", "invalid_feed_url", "")


def test_unknown_values_are_stored_as_null_not_empty_strings(db, tmp_path):
    run_import(db, make_dump(tmp_path, baseline_rows()))

    empty_values = db.execute(
        "select count(*) from source_records, jsonb_each_text(attrs) kv where kv.value = ''"
    ).fetchone()[0]
    untitled = db.execute(
        "select display_name from source_records where source_key = '6'"
    ).fetchone()[0]

    assert empty_values == 0
    assert untitled is None


def test_failed_import_rolls_back_and_the_rerun_reuses_the_run(db, tmp_path):
    path = make_dump(tmp_path, baseline_rows())

    def broken_resolver(*args, **kwargs):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        run_import(db, path, resolver=broken_resolver)
    interrupted = db.execute("select run_id, status from import_runs").fetchone()
    assert counts(db)["source_records"] == 0
    assert interrupted[1] == "running"

    report = run_import(db, path)

    assert (report.run_id, report.status) == (interrupted[0], "ok")
    assert counts(db)["source_records"] == 11


def test_resume_after_a_crash_past_commit_keeps_the_real_metrics(db, tmp_path):
    path = make_dump(tmp_path, baseline_rows())
    first = run_import(db, path)
    # Simulate a crash after the data commit but before the run was marked finished.
    db.execute(
        "update import_runs set status = 'running', finished_at = null where run_id = %s",
        (first.run_id,),
    )
    before = counts(db)

    resumed = run_import(db, path)

    assert (resumed.run_id, resumed.status, resumed.skipped) == (first.run_id, "ok", False)
    assert resumed.metrics.rows_new == first.metrics.rows_new == 11
    assert resumed.metrics.entities_created == first.metrics.entities_created
    assert counts(db) == before


def test_renumbered_feeds_are_a_different_input(db, tmp_path):
    rows = [dump_row(1, title="A"), dump_row(2, title="B")]
    first = run_import(db, make_dump(tmp_path, rows))
    swapped_dir = tmp_path / "swapped"
    swapped_dir.mkdir()
    swapped = [
        dump_row(1, title="B", url=rows[1]["url"]),
        dump_row(2, title="A", url=rows[0]["url"]),
    ]

    second = run_import(db, make_dump(swapped_dir, swapped))

    assert second.skipped is False
    assert second.run_id != first.run_id
    assert second.metrics.rows_changed == 2


def test_empty_slice_finishes_ok_with_zero_counts(db, tmp_path):
    report = run_import(db, make_dump(tmp_path, [dump_row(1, language="fr")]))

    assert report.status == "ok"
    assert (report.metrics.rows_in, report.metrics.rows_new) == (0, 0)
    assert report.metrics.entities_created == 0
