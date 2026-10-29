from pathlib import Path

import psycopg
import pytest

from ddm.db.migrate import MigrationError, discover, migrate

EXPECTED_TABLES = {
    "schema_migrations",
    "sources",
    "import_runs",
    "run_metrics",
    "import_rejects",
    "entities",
    "entity_identifiers",
    "source_records",
    "observations",
    "candidate_matches",
    "ambiguous_identifiers",
    "feed_fetch_state",
    "fetch_attempts",
    "contact_routes",
    "discoveries",
    "discovery_cursors",
    "source_backoff",
    "api_keys",
    "api_usage",
}


def write(directory: Path, name: str, body: str) -> None:
    (directory / name).write_text(body, encoding="utf-8")


def test_discover_returns_bundled_migrations_in_version_order():
    migrations = discover()

    assert [(m.version, m.name) for m in migrations] == [
        (1, "extensions_and_runs"),
        (2, "records_and_entities"),
        (3, "podcast_refresh"),
        (4, "discovery"),
        (5, "journalists"),
        (6, "query_layer"),
    ]
    assert all(len(m.checksum) == 64 for m in migrations)


def test_discover_rejects_badly_named_files(tmp_path):
    write(tmp_path, "0003_Bad Name.sql", "select 1;")

    with pytest.raises(MigrationError, match="0003_Bad Name.sql"):
        discover(tmp_path)


def test_discover_rejects_duplicate_versions(tmp_path):
    write(tmp_path, "0001_a.sql", "select 1;")
    write(tmp_path, "0001_b.sql", "select 1;")

    with pytest.raises(MigrationError, match="duplicate migration version 1"):
        discover(tmp_path)


def test_discover_ignores_non_sql_files(tmp_path):
    write(tmp_path, "0001_a.sql", "select 1;")
    write(tmp_path, "README.md", "notes")

    assert [m.name for m in discover(tmp_path)] == ["a"]


@pytest.mark.integration
def test_migrate_creates_every_table_on_an_empty_database(fresh_db):
    applied = migrate(fresh_db)

    tables = {
        row[0]
        for row in fresh_db.execute(
            "select tablename from pg_tables where schemaname = 'public'"
        ).fetchall()
    }
    assert [m.version for m in applied] == [1, 2, 3, 4, 5, 6]
    assert tables == EXPECTED_TABLES


@pytest.mark.integration
def test_migrate_is_a_no_op_the_second_time(fresh_db):
    migrate(fresh_db)

    assert migrate(fresh_db) == ()


@pytest.mark.integration
def test_migrate_refuses_when_an_applied_file_was_modified(fresh_db, tmp_path):
    write(tmp_path, "0001_first.sql", "create table t1 (id int);")
    migrate(fresh_db, discover(tmp_path))
    write(tmp_path, "0001_first.sql", "create table t1 (id bigint);")

    with pytest.raises(MigrationError, match="0001 was modified"):
        migrate(fresh_db, discover(tmp_path))


@pytest.mark.integration
def test_migrate_applies_only_new_versions(fresh_db, tmp_path):
    write(tmp_path, "0001_first.sql", "create table t1 (id int);")
    migrate(fresh_db, discover(tmp_path))
    write(tmp_path, "0002_second.sql", "create table t2 (id int);")

    applied = migrate(fresh_db, discover(tmp_path))

    assert [m.version for m in applied] == [2]


@pytest.mark.integration
def test_migrate_refuses_a_new_version_older_than_the_latest_applied(fresh_db, tmp_path):
    write(tmp_path, "0002_second.sql", "create table t2 (id int);")
    migrate(fresh_db, discover(tmp_path))
    write(tmp_path, "0001_first.sql", "create table t1 (id int);")

    with pytest.raises(MigrationError, match="0001 is older than applied version 0002"):
        migrate(fresh_db, discover(tmp_path))


@pytest.mark.integration
def test_migrate_refuses_when_an_applied_file_is_missing(fresh_db, tmp_path):
    write(tmp_path, "0001_first.sql", "create table t1 (id int);")
    migrate(fresh_db, discover(tmp_path))
    (tmp_path / "0001_first.sql").unlink()
    write(tmp_path, "0002_second.sql", "create table t2 (id int);")

    with pytest.raises(MigrationError, match="applied migration 0001 is missing"):
        migrate(fresh_db, discover(tmp_path))


def test_discover_rejects_a_name_with_a_trailing_newline(tmp_path):
    write(tmp_path, "0001_a.sql\n", "select 1;")

    with pytest.raises(MigrationError):
        discover(tmp_path)


@pytest.mark.integration
def test_failed_migration_rolls_back_and_is_not_recorded(fresh_db, tmp_path):
    write(tmp_path, "0001_broken.sql", "create table t1 (id int); select * from missing;")

    with pytest.raises(psycopg.errors.UndefinedTable):
        migrate(fresh_db, discover(tmp_path))

    assert fresh_db.execute("select to_regclass('t1')").fetchone()[0] is None
    assert fresh_db.execute("select count(*) from schema_migrations").fetchone()[0] == 0


@pytest.mark.integration
def test_observations_reject_update_and_delete(fresh_db):
    migrate(fresh_db)
    with fresh_db.transaction():
        run_id = fresh_db.execute(
            """insert into import_runs
                 (source_id, run_kind, input_fingerprint, is_complete_snapshot)
               values (1, 'historical_backfill', 'fp', true) returning run_id"""
        ).fetchone()[0]
        fresh_db.execute(
            """insert into observations
                 (run_id, source_id, source_key, change_kind, provenance, payload)
               values (%s, 1, 'k', 'new', 'historical', '{}')""",
            (run_id,),
        )

    for statement in (
        "update observations set source_key = 'x'",
        "delete from observations",
        "truncate observations",
    ):
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            with fresh_db.transaction():
                fresh_db.execute(statement)


@pytest.mark.integration
def test_norm_text_unaccents_lowercases_and_collapses_whitespace(fresh_db):
    migrate(fresh_db)

    value = fresh_db.execute("select norm_text('  Café   Société ')").fetchone()[0]

    assert value == "cafe societe"


@pytest.mark.integration
def test_sources_are_seeded_with_licence_and_access(fresh_db):
    migrate(fresh_db)

    row = fresh_db.execute(
        "select source_id, license from sources where name = 'podcastindex_dump'"
    ).fetchone()

    assert row == (1, "MIT")


@pytest.mark.integration
def test_podcast_rss_source_is_seeded(fresh_db):
    migrate(fresh_db)

    row = fresh_db.execute("select source_id from sources where name = 'podcast_rss'").fetchone()

    assert row == (2,)


@pytest.mark.integration
def test_contact_routes_reject_unknown_purpose_and_empty_value(fresh_db):
    migrate(fresh_db)
    with fresh_db.transaction():
        run_id = fresh_db.execute(
            """insert into import_runs
                 (source_id, run_kind, input_fingerprint, is_complete_snapshot)
               values (2, 'refresh', 'fp', false) returning run_id"""
        ).fetchone()[0]
        entity_id = fresh_db.execute(
            """insert into entities (entity_id, entity_type, first_seen_kind, first_seen_run)
               values (gen_random_uuid(), 'podcast', 'observed', %s) returning entity_id""",
            (run_id,),
        ).fetchone()[0]
    insert = """insert into contact_routes
        (entity_id, route_type, value, syntax_valid, affiliation_status, affiliation_basis,
         purpose, purpose_basis, source_id, source_key, provenance, first_seen_run,
         checked_run, checked_at)
        values (%s, 'website', %s, true, 'confirmed', 'b', %s, 'b', 2, '1', 'observed',
                %s, %s, now())"""

    for value, purpose in (("https://a.example.com", "marketing"), ("", "general")):
        with pytest.raises(psycopg.errors.CheckViolation):
            with fresh_db.transaction():
                fresh_db.execute(insert, (entity_id, value, purpose, run_id, run_id))


@pytest.mark.integration
def test_fetch_attempts_require_a_reason_unless_the_fetch_succeeded(fresh_db):
    migrate(fresh_db)
    with fresh_db.transaction():
        run_id = fresh_db.execute(
            """insert into import_runs
                 (source_id, run_kind, input_fingerprint, is_complete_snapshot)
               values (2, 'refresh', 'fp', false) returning run_id"""
        ).fetchone()[0]
    insert = """insert into fetch_attempts
        (run_id, source_key, url, outcome, reason, bytes, elapsed_ms, attempted_at)
        values (%s, %s, 'https://a.example.com/f', %s, %s, 0, 0, now())"""

    for key, outcome, reason in (("1", "failed", None), ("2", "fetched", "x")):
        with pytest.raises(psycopg.errors.CheckViolation):
            with fresh_db.transaction():
                fresh_db.execute(insert, (run_id, key, outcome, reason))


@pytest.mark.integration
def test_reader_role_cannot_read_contact_routes_but_can_read_the_fetch_log(reader_db):
    migrate(reader_db)

    def can_read(table):
        return reader_db.execute(
            "select has_table_privilege('ddm_reader', %s, 'SELECT')", (table,)
        ).fetchone()[0]

    assert can_read("contact_routes") is False
    assert can_read("fetch_attempts") is True


@pytest.mark.integration
def test_migrations_give_up_quickly_when_a_table_is_locked(fresh_db, pg_url, tmp_path):
    write(tmp_path, "0001_first.sql", "create table t1 (id int);")
    migrate(fresh_db, discover(tmp_path))
    write(tmp_path, "0002_second.sql", "alter table t1 add column x int;")

    with psycopg.connect(pg_url) as holder:
        holder.execute("lock table t1 in access exclusive mode")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            migrate(fresh_db, discover(tmp_path))


def _discovery_fixture(conn, fingerprint="fp"):
    """A discovery run with two entities, for constraint tests."""
    with conn.transaction():
        run_id = conn.execute(
            """insert into import_runs
                 (source_id, run_kind, input_fingerprint, is_complete_snapshot)
               values (1, 'discovery', %s, false) returning run_id""",
            (fingerprint,),
        ).fetchone()[0]
        a, b = (
            conn.execute(
                """insert into entities (entity_id, entity_type, first_seen_kind, first_seen_run)
                   values (gen_random_uuid(), 'podcast', 'discovered', %s) returning entity_id""",
                (run_id,),
            ).fetchone()[0]
            for _ in range(2)
        )
    return run_id, a, b


@pytest.mark.integration
def test_podcastindex_api_source_is_seeded(fresh_db):
    migrate(fresh_db)

    row = fresh_db.execute(
        "select source_id from sources where name = 'podcastindex_api'"
    ).fetchone()

    assert row == (3,)


@pytest.mark.integration
def test_entities_accept_discovered_but_no_other_new_kind(fresh_db):
    migrate(fresh_db)
    run_id, _, _ = _discovery_fixture(fresh_db)

    with pytest.raises(psycopg.errors.CheckViolation):
        fresh_db.execute(
            """insert into entities (entity_id, entity_type, first_seen_kind, first_seen_run)
               values (gen_random_uuid(), 'podcast', 'other', %s)""",
            (run_id,),
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("classification", "entity", "matched"),
    [
        ("new", "a", "b"),  # a new entity matches nothing
        ("uncertain", "a", None),  # an uncertain record names its suspect
        ("duplicate", "a", "b"),  # an attached record shares the matched entity
        ("uncertain", "a", "a"),  # an uncertain record got its own entity
    ],
)
def test_discoveries_enforce_how_classification_and_entities_relate(
    fresh_db, classification, entity, matched
):
    migrate(fresh_db)
    run_id, a, b = _discovery_fixture(fresh_db)
    ids = {"a": a, "b": b, None: None}

    with pytest.raises(psycopg.errors.CheckViolation):
        fresh_db.execute(
            """insert into discoveries
                 (run_id, source_id, source_key, classification, entity_id, matched_entity_id,
                  method, evidence, classifier_version)
               values (%s, 1, '1', %s, %s, %s, 'm', '{}', 1)""",
            (run_id, classification, ids[entity], ids[matched]),
        )


@pytest.mark.integration
def test_discovery_cursor_must_be_a_positive_feed_id(fresh_db):
    migrate(fresh_db)
    run_id, _, _ = _discovery_fixture(fresh_db)

    with pytest.raises(psycopg.errors.CheckViolation):
        fresh_db.execute(
            "insert into discovery_cursors (source_id, last_feed_id, updated_run)"
            " values (3, 0, %s)",
            (run_id,),
        )


@pytest.mark.integration
def test_entity_names_have_a_gist_index_for_nearest_neighbour_search(fresh_db):
    migrate(fresh_db)

    row = fresh_db.execute(
        "select indexdef from pg_indexes where indexname = 'entities_norm_name_gist'"
    ).fetchone()

    assert row is not None and "gist_trgm_ops" in row[0]


@pytest.mark.integration
def test_a_record_can_be_classified_only_once(fresh_db):
    migrate(fresh_db)
    run_id, a, _ = _discovery_fixture(fresh_db)
    insert = """insert into discoveries
        (run_id, source_id, source_key, classification, entity_id, method, evidence,
         classifier_version)
        values (%s, 1, '1', 'new', %s, 'm', '{}', 1)"""
    fresh_db.execute(insert, (run_id, a))
    other_run, _, _ = _discovery_fixture(fresh_db, fingerprint="fp2")

    with pytest.raises(psycopg.errors.UniqueViolation):
        fresh_db.execute(insert, (other_run, a))


@pytest.mark.integration
def test_backoff_only_records_refusals(fresh_db):
    migrate(fresh_db)
    run_id, _, _ = _discovery_fixture(fresh_db)

    with pytest.raises(psycopg.errors.CheckViolation):
        fresh_db.execute(
            "insert into source_backoff (source_id, retry_after, http_status, set_run)"
            " values (3, now(), 500, %s)",
            (run_id,),
        )


@pytest.mark.integration
def test_the_unused_gin_name_index_is_gone(fresh_db):
    migrate(fresh_db)

    row = fresh_db.execute(
        "select 1 from pg_indexes where indexname = 'entities_norm_name_trgm'"
    ).fetchone()

    assert row is None


@pytest.mark.integration
def test_wikidata_and_outlet_rss_sources_are_seeded(fresh_db):
    migrate(fresh_db)

    rows = fresh_db.execute(
        "select source_id, name from sources where source_id in (4, 5) order by source_id"
    ).fetchall()

    assert rows == [(4, "wikidata"), (5, "outlet_rss")]


@pytest.mark.integration
@pytest.mark.parametrize("table", ["entity_identifiers", "ambiguous_identifiers"])
def test_identifier_tables_accept_outlet_author_urls_but_no_unknown_kind(fresh_db, table):
    migrate(fresh_db)
    run_id, a, _ = _discovery_fixture(fresh_db)
    if table == "entity_identifiers":
        insert = """insert into entity_identifiers
                      (id_kind, id_value, entity_id, source_id, added_run)
                    values (%s, 'theatlantic.com/author/a-b', %s, 1, %s)"""
    else:
        insert = """insert into ambiguous_identifiers
                      (id_kind, id_value, record_count, source_id, first_run)
                    values (%s, 'theatlantic.com/author/a-b', 2, 1, %s)"""
    params = (a, run_id) if table == "entity_identifiers" else (run_id,)

    fresh_db.execute(insert, ("outlet_author_url", *params))
    with pytest.raises(psycopg.errors.CheckViolation):
        fresh_db.execute(insert, ("muckrack_id", *params))


@pytest.mark.integration
def test_run_metrics_count_soft_deleted_rows(fresh_db):
    migrate(fresh_db)

    row = fresh_db.execute(
        "select 1 from information_schema.columns"
        " where table_name = 'run_metrics' and column_name = 'rows_deleted'"
    ).fetchone()

    assert row == (1,)
