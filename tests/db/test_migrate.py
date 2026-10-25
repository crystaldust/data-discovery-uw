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
}


def write(directory: Path, name: str, body: str) -> None:
    (directory / name).write_text(body, encoding="utf-8")


def test_discover_returns_bundled_migrations_in_version_order():
    migrations = discover()

    assert [(m.version, m.name) for m in migrations] == [
        (1, "extensions_and_runs"),
        (2, "records_and_entities"),
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
    assert [m.version for m in applied] == [1, 2]
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
