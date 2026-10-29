"""Migration 0006: the search view, masked contact routes, and the read-only role's rights."""

import hashlib
from datetime import UTC, datetime

import psycopg
import pytest

from ddm.db.migrate import migrate
from tests import seed

pytestmark = pytest.mark.integration


@pytest.fixture
def db(reader_db):
    migrate(reader_db)
    return reader_db


@pytest.fixture
def reader(db, reader_url):
    with psycopg.connect(reader_url, autocommit=True) as conn:
        yield conn


def read_write(conn, sql, params=()):
    """What the app does for metering: the role's sessions default to read-only."""
    with conn.transaction():
        conn.execute("SET TRANSACTION READ WRITE")
        return conn.execute(sql, params).fetchone()


def search_row(conn, entity_id):
    cur = conn.execute("SELECT * FROM entity_search WHERE entity_id = %s", (entity_id,))
    names = [c.name for c in cur.description]
    return dict(zip(names, cur.fetchone(), strict=True))


@pytest.mark.parametrize(
    ("value", "masked"),
    [
        ("jane@example.org", "j***@example.org"),
        ("abc@b.c", "***@b.c"),
        ("abcd@b.c", "a***@b.c"),
        ("odd@name@example.org", "o***@example.org"),
        ("@example.org", "***"),
        ("no-at-sign", "***"),
    ],
)
def test_mask_email_keeps_at_most_the_first_character_and_the_domain(db, value, masked):
    assert db.execute("SELECT mask_email(%s)", (value,)).fetchone()[0] == masked


def test_public_contact_routes_mask_emails_and_keep_urls(db):
    run = seed.run(db, 2)
    eid = seed.entity(db, "podcast", "Show", run)
    seed.route(db, eid, "feed_owner_email", seed.SECRET_EMAIL, run)
    seed.route(db, eid, "website", "https://show.example.com", run)

    rows = dict(
        db.execute(
            "SELECT route_type, display_value FROM contact_routes_public ORDER BY route_id"
        ).fetchall()
    )
    columns = {
        r[0]
        for r in db.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'contact_routes_public'"
        ).fetchall()
    }

    assert rows == {"feed_owner_email": "j***@example.org", "website": "https://show.example.com"}
    assert "value" not in columns


def test_search_row_prefers_rss_fields_and_records_where_activity_came_from(db):
    dump_run = seed.run(db, 1, "2026-10-25T00:00:00Z", kind="historical_backfill")
    rss_run = seed.run(db, 2, "2026-10-27T00:00:00Z")
    eid = seed.entity(db, "podcast", "B2B Growth", dump_run)
    seed.record(
        db,
        1,
        "1",
        eid,
        dump_run,
        {
            "language": "en-US",
            "categories": ["marketing"],
            "newest_item_at": "2025-01-01T00:00:00+00:00",
            "owner_email": seed.SECRET_EMAIL,
        },
    )
    seed.record(
        db,
        2,
        "1",
        eid,
        rss_run,
        {
            "language": "EN-gb",
            "categories": ["Business", "Marketing"],
            "last_episode_at": "2026-10-20T00:00:00+00:00",
            "owner_email": seed.SECRET_EMAIL,
        },
        provenance="observed",
    )
    seed.route(db, eid, "feed_owner_email", seed.SECRET_EMAIL, rss_run, source_key="1")
    seed.refresh(db)

    row = search_row(db, eid)

    assert row["language"] == "en"
    assert row["categories"] == ["business", "marketing"]
    assert row["last_activity_at"] == datetime(2026, 10, 20, tzinfo=UTC)
    assert row["activity_basis"] == "observed"
    assert row["last_checked_at"] == datetime(2026, 10, 27, tzinfo=UTC)
    assert row["imported_at"] == datetime(2026, 10, 25, tzinfo=UTC)
    assert row["contact_routes"] == 1
    assert row["source_ids"] == [1, 2]
    assert row["all_deleted"] is False
    assert seed.SECRET_EMAIL not in repr(row)


def test_search_row_falls_back_to_the_dump_when_there_is_no_rss_record(db):
    run = seed.run(db, 1, kind="historical_backfill")
    eid = seed.entity(db, "podcast", "Old Show", run)
    seed.record(
        db,
        1,
        "2",
        eid,
        run,
        {
            "language": "en_us",
            "newest_item_at": "2023-05-01T00:00:00+00:00",
        },
    )
    seed.refresh(db)

    row = search_row(db, eid)

    assert row["activity_basis"] == "historical"
    assert row["language"] == "en"
    assert row["last_activity_at"] == datetime(2023, 5, 1, tzinfo=UTC)
    assert row["last_checked_at"] is None
    assert row["categories"] == []


def test_journalist_row_has_outlets_from_both_sources_and_the_newest_byline(db):
    wd_run = seed.run(db, 4, kind="refresh")
    rss_run = seed.run(db, 5)
    eid = seed.entity(db, "journalist", "Some Reporter", wd_run)
    seed.record(
        db,
        4,
        "Q1",
        eid,
        wd_run,
        {
            "employments": [{"outlet_qid": "Q9", "outlet": "The New York Times"}],
        },
        provenance="observed",
    )
    seed.record(
        db,
        5,
        "theatlantic.com/author/a",
        eid,
        rss_run,
        {
            "outlet": "The Atlantic",
            "latest_byline_at": "2026-10-28T10:00:00+00:00",
        },
        provenance="observed",
    )
    seed.record(
        db,
        5,
        "politico.com/staff/a",
        eid,
        rss_run,
        {
            "outlet": "Politico",
            "latest_byline_at": "2026-09-01T10:00:00+00:00",
        },
        provenance="observed",
    )
    seed.refresh(db)

    row = search_row(db, eid)

    assert row["outlets"] == ["Politico", "The Atlantic", "The New York Times"]
    assert row["last_activity_at"] == datetime(2026, 10, 28, 10, tzinfo=UTC)
    assert row["language"] is None


def test_an_entity_whose_records_are_all_deleted_is_marked(db):
    run = seed.run(db, 4)
    gone = seed.run(db, 4, "2026-10-28T00:00:00Z")
    eid = seed.entity(db, "journalist", "Gone", run)
    seed.record(db, 4, "Q2", eid, run, {"employments": []}, deleted_run=gone)
    seed.refresh(db)

    assert search_row(db, eid)["all_deleted"] is True


def test_reader_can_read_the_views_but_not_contact_routes_or_keys(reader):
    assert reader.execute("SELECT count(*) FROM entity_search").fetchone()[0] == 0
    assert reader.execute("SELECT count(*) FROM contact_routes_public").fetchone()[0] == 0
    for table in ("contact_routes", "api_keys", "api_usage"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            reader.execute(f"SELECT 1 FROM {table}")  # noqa: S608 - fixed table names


def test_reader_cannot_write_tables_even_in_a_read_write_transaction(db, reader):
    key_id = db.execute(
        "INSERT INTO api_keys (label, key_hash) VALUES ('t', %s) RETURNING key_id",
        (hashlib.sha256(b"k").digest(),),
    ).fetchone()[0]

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        read_write(reader, "INSERT INTO api_usage VALUES (%s, now(), 'api', 'x', 1)", (key_id,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        read_write(reader, "UPDATE entities SET display_name = 'x' RETURNING 1")


def test_reader_sessions_default_to_read_only_with_a_statement_timeout(reader):
    assert reader.execute("SHOW default_transaction_read_only").fetchone()[0] == "on"
    assert reader.execute("SHOW statement_timeout").fetchone()[0] == "5s"
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        reader.execute("CREATE TEMP TABLE t (id int)")


def test_a_malformed_timestamp_in_attrs_does_not_break_the_view(db):
    run = seed.run(db, 1, kind="historical_backfill")
    eid = seed.entity(db, "podcast", "Odd Dates", run)
    seed.record(db, 1, "odd", eid, run, {"newest_item_at": "last Tuesday"})

    db.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY entity_search")

    assert search_row(db, eid)["last_activity_at"] is None


def test_reader_can_check_a_key_and_meter_it(db, reader, fresh_minute):
    digest = hashlib.sha256(b"key").digest()
    key_id = db.execute(
        "INSERT INTO api_keys (label, key_hash, rate_limit_per_minute) "
        "VALUES ('t', %s, 2) RETURNING key_id",
        (digest,),
    ).fetchone()[0]

    found = reader.execute("SELECT api_key_check(%s)", (digest,)).fetchone()[0]
    results = [
        read_write(reader, "SELECT * FROM api_meter(%s, 'api', 'entities')", (key_id,))
        for _ in range(3)
    ]
    calls = db.execute("SELECT sum(calls) FROM api_usage").fetchone()[0]

    assert found == key_id
    assert results[0] == (True, 0) and results[1] == (True, 0)
    assert results[2][0] is False and 1 <= results[2][1] <= 60
    assert calls == 3


def test_a_revoked_key_is_neither_found_nor_metered(db, reader):
    digest = hashlib.sha256(b"old").digest()
    key_id = db.execute(
        "INSERT INTO api_keys (label, key_hash, revoked_at) VALUES ('t', %s, now()) "
        "RETURNING key_id",
        (digest,),
    ).fetchone()[0]

    assert reader.execute("SELECT api_key_check(%s)", (digest,)).fetchone()[0] is None
    metered = read_write(reader, "SELECT allowed FROM api_meter(%s, 'mcp', 'ping')", (key_id,))
    assert metered[0] is False


def test_other_roles_cannot_call_the_key_functions(db):
    rows = db.execute(
        "SELECT proname FROM pg_proc WHERE proname IN ('api_key_check', 'api_meter') "
        "AND has_function_privilege('public', oid, 'EXECUTE')"
    ).fetchall()

    assert rows == []


def test_the_search_view_can_be_refreshed_concurrently(db):
    db.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY entity_search")


def test_the_view_refreshes_when_the_planner_chooses_parallel_workers(db):
    run = seed.run(db, 1, kind="historical_backfill")
    for i in range(50):
        eid = seed.entity(db, "podcast", f"P{i}", run)
        seed.record(db, 1, f"p{i}", eid, run, {"newest_item_at": "2026-01-01T00:00:00+00:00"})
    for setting in (
        "parallel_setup_cost = 0",
        "parallel_tuple_cost = 0",
        "min_parallel_table_scan_size = 0",
        "debug_parallel_query = on",
    ):
        db.execute(f"SET {setting}")
    try:
        db.execute("REFRESH MATERIALIZED VIEW entity_search")
    finally:
        db.execute("RESET ALL")

    assert db.execute("SELECT count(last_activity_at) FROM entity_search").fetchone()[0] == 50
