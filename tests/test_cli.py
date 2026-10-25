from datetime import date

import pytest

from ddm.cli import build_parser, main
from tests.conftest import dump_row, make_dump

SECRET_URL = "postgresql://owner:hunter2@db.example.com/prod"


def test_parser_reads_import_baseline_arguments(tmp_path):
    args = build_parser().parse_args(
        ["import-baseline", "--dump", str(tmp_path / "x.db"), "--cutoff", "2023-06-01"]
    )

    assert args.command == "import-baseline"
    assert args.dump == tmp_path / "x.db"
    assert args.cutoff == date(2023, 6, 1)


def test_parser_defaults_cutoff_to_the_baseline_date(tmp_path):
    args = build_parser().parse_args(["import-baseline", "--dump", "x.db"])

    assert args.cutoff == date(2024, 1, 1)


def test_invalid_cutoff_exits_with_usage_error():
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["import-baseline", "--dump", "x.db", "--cutoff", "2024-13-01"])

    assert excinfo.value.code == 2


def test_missing_command_exits_with_usage_error():
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args([])

    assert excinfo.value.code == 2


def test_missing_database_url_fails_without_printing_urls(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert main(["migrate"]) == 1

    output = capsys.readouterr()
    assert "DATABASE_URL" in output.err
    assert "postgresql://" not in output.out + output.err


def test_missing_dump_file_fails_before_connecting(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("DATABASE_URL", SECRET_URL)

    assert main(["import-baseline", "--dump", str(tmp_path / "missing.db")]) == 1

    output = capsys.readouterr()
    assert "dump not found" in output.err
    assert "hunter2" not in output.out + output.err


def test_connection_failure_never_prints_the_password(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://owner:s3cret-pw@127.0.0.1:1/db")

    assert main(["migrate"]) == 1

    output = capsys.readouterr()
    assert "database operation failed (OperationalError)" in output.err
    assert "s3cret-pw" not in output.out + output.err


def test_malformed_url_never_prints_the_password(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://owner:s3cret%zz@127.0.0.1:1/db")

    assert main(["migrate"]) == 1

    assert "s3cret" not in "".join(capsys.readouterr())


def test_pooled_neon_url_is_refused_for_jobs(monkeypatch, capsys):
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://owner:pw@ep-cool-1-pooler.us-east-1.aws.neon.tech/db"
    )

    assert main(["migrate"]) == 1

    assert "direct (non-pooled)" in capsys.readouterr().err


@pytest.mark.integration
def test_migrate_then_import_twice_reports_skip(monkeypatch, capsys, fresh_db, pg_url, tmp_path):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    path = make_dump(tmp_path, [dump_row(1), dump_row(2, itunesId=5), dump_row(3, itunesId=5)])

    assert main(["migrate"]) == 0
    assert main(["migrate"]) == 0
    assert main(["import-baseline", "--dump", str(path)]) == 0
    assert main(["import-baseline", "--dump", str(path)]) == 0

    out = capsys.readouterr().out
    assert "applied version=1 name=extensions_and_runs" in out
    assert "schema up to date version=2" in out
    assert "rows_in=3 rejected=0 new=3 changed=0 unchanged=0" in out
    assert "entities_created=2" in out
    assert "strong_id_merges=1" in out
    assert "already imported (same input fingerprint), nothing written" in out
    assert "ddm-local-only" not in out
