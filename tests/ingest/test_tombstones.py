import pytest

from ddm.db.migrate import migrate
from ddm.ingest.records import make_record
from ddm.ingest.runs import finish_run, register_run
from ddm.ingest.tombstones import (
    MIN_DELETE_GUARD,
    MISSED_SNAPSHOTS_BEFORE_DELETE,
    mark_missing_deleted,
)
from ddm.ingest.upsert import upsert_records

pytestmark = pytest.mark.integration

WIKIDATA = 4


@pytest.fixture
def db(fresh_db):
    migrate(fresh_db)
    return fresh_db


def record(key, name="Avery Example"):
    return make_record(
        source_key=key, display_name=name, attrs={}, source_updated_at=None, identifiers=()
    )


def snapshot(conn, keys, *, number, status="ok", complete=True, allow=True):
    """One snapshot run that saw `keys`; returns (run_id, keys soft-deleted by it)."""
    run = register_run(
        conn,
        source_id=WIKIDATA,
        run_kind="refresh",
        fingerprint=f"fp-{number}",
        is_complete_snapshot=complete,
        params={},
    )
    deleted = 0
    with conn.transaction():
        upsert_records(
            conn,
            source_id=WIKIDATA,
            run_id=run.run_id,
            provenance="observed",
            records=[record(k) for k in keys],
        )
        if complete and status == "ok":
            result = mark_missing_deleted(conn, source_id=WIKIDATA, run_id=run.run_id, allow=allow)
            deleted = result.deleted if allow else result.withheld
    finish_run(conn, run.run_id, status)
    return run.run_id, deleted


def deleted_keys(conn):
    rows = conn.execute(
        "select source_key from source_records where deleted_at is not null order by 1"
    ).fetchall()
    return [r[0] for r in rows]


def test_the_grace_period_is_two_snapshots():
    assert MISSED_SNAPSHOTS_BEFORE_DELETE == 2


def test_a_record_missing_once_is_kept(db):
    snapshot(db, ["Q1", "Q2"], number=1)

    _, deleted = snapshot(db, ["Q1"], number=2)

    assert deleted == 0
    assert deleted_keys(db) == []


def test_a_record_missing_from_two_complete_snapshots_is_soft_deleted(db):
    snapshot(db, ["Q1", "Q2"], number=1)
    snapshot(db, ["Q1"], number=2)

    run_id, deleted = snapshot(db, ["Q1"], number=3)

    assert deleted == 1
    assert deleted_keys(db) == ["Q2"]
    row = db.execute(
        "select change_kind, payload, content_hash from observations"
        " where run_id = %s and source_key = 'Q2'",
        (run_id,),
    ).fetchone()
    assert row == ("deleted", None, None)
    deleted_run = db.execute("select deleted_run from source_records where source_key = 'Q2'")
    assert deleted_run.fetchone() == (run_id,)


def test_a_partial_run_in_between_does_not_count(db):
    snapshot(db, ["Q1", "Q2"], number=1)
    snapshot(db, ["Q1"], number=2, status="partial")

    _, deleted = snapshot(db, ["Q1"], number=3)

    assert deleted == 0


def test_nothing_is_deleted_with_only_one_complete_snapshot(db):
    snapshot(db, ["Q1"], number=1, complete=False)

    _, deleted = snapshot(db, ["Q2"], number=2)

    assert deleted == 0


def test_a_deleted_record_that_comes_back_is_revived_as_a_change(db):
    snapshot(db, ["Q1", "Q2"], number=1)
    snapshot(db, ["Q1"], number=2)
    snapshot(db, ["Q1"], number=3)

    run_id, _ = snapshot(db, ["Q1", "Q2"], number=4)

    assert deleted_keys(db) == []
    kind = db.execute(
        "select change_kind from observations where run_id = %s and source_key = 'Q2'",
        (run_id,),
    ).fetchone()
    assert kind == ("changed",)


def test_an_already_deleted_record_is_not_deleted_again(db):
    snapshot(db, ["Q1", "Q2"], number=1)
    snapshot(db, ["Q1"], number=2)
    snapshot(db, ["Q1"], number=3)

    _, deleted = snapshot(db, ["Q1"], number=4)

    assert deleted == 0


def test_other_sources_are_untouched(db):
    other = register_run(
        db,
        source_id=1,
        run_kind="historical_backfill",
        fingerprint="dump",
        is_complete_snapshot=True,
        params={},
    )
    with db.transaction():
        upsert_records(
            db, source_id=1, run_id=other.run_id, provenance="historical", records=[record("1")]
        )
    finish_run(db, other.run_id, "ok")
    snapshot(db, ["Q1"], number=1)
    snapshot(db, ["Q1"], number=2)

    assert deleted_keys(db) == []


def test_it_must_run_inside_a_transaction(db):
    with pytest.raises(RuntimeError, match="transaction"):
        mark_missing_deleted(db, source_id=WIKIDATA, run_id=1)


def test_an_unusually_large_deletion_is_withheld(db):
    keys = [f"Q{n}" for n in range(1, MIN_DELETE_GUARD + 3)]
    snapshot(db, keys, number=1)
    snapshot(db, ["Q1"], number=2)

    run = register_run(
        db,
        source_id=WIKIDATA,
        run_kind="refresh",
        fingerprint="fp-3",
        is_complete_snapshot=True,
        params={},
    )
    with db.transaction():
        upsert_records(
            db, source_id=WIKIDATA, run_id=run.run_id, provenance="observed", records=[record("Q1")]
        )
        result = mark_missing_deleted(db, source_id=WIKIDATA, run_id=run.run_id)

    assert (result.deleted, result.withheld) == (0, MIN_DELETE_GUARD + 1)
    assert deleted_keys(db) == []


def test_a_caller_can_withhold_deletion_and_still_learn_the_count(db):
    snapshot(db, ["Q1", "Q2"], number=1)
    snapshot(db, ["Q1"], number=2)

    _, withheld = snapshot(db, ["Q1"], number=3, allow=False)

    assert withheld == 1
    assert deleted_keys(db) == []
