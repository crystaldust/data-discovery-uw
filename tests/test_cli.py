from datetime import date, timedelta

import pytest

from ddm.cli import build_parser, main
from tests.conftest import dump_row, make_dump
from tests.ingest.test_refresh import FakeFetcher, rss

HONEST_UA = "DataDiscoveryDemo/0.1 (+mailto:test@example.com)"

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
    assert "schema up to date version=3" in out
    assert "rows_in=3 rejected=0 new=3 changed=0 unchanged=0" in out
    assert "entities_created=2" in out
    assert "strong_id_merges=1" in out
    assert "already imported (same input fingerprint), nothing written" in out
    assert "ddm-local-only" not in out


def test_parser_reads_refresh_arguments_with_defaults():
    defaults = build_parser().parse_args(["refresh-podcasts"])
    explicit = build_parser().parse_args(["refresh-podcasts", "--limit", "50", "--same-as", "7"])

    assert (defaults.limit, defaults.same_as) == (1000, None)
    assert (explicit.limit, explicit.same_as) == (50, 7)


def test_refresh_without_user_agent_fails_naming_the_variable(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", SECRET_URL)
    monkeypatch.delenv("DDM_USER_AGENT", raising=False)

    assert main(["refresh-podcasts"]) == 1

    err = capsys.readouterr().err
    assert "DDM_USER_AGENT" in err
    assert "hunter2" not in err


def test_refresh_with_a_browser_user_agent_is_refused(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", SECRET_URL)
    monkeypatch.setenv("DDM_USER_AGENT", "Mozilla/5.0 (+mailto:a@example.com)")

    assert main(["refresh-podcasts"]) == 1

    assert "honestly" in capsys.readouterr().err


def test_refresh_limit_out_of_range_fails_before_connecting(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", SECRET_URL)
    monkeypatch.setenv("DDM_USER_AGENT", HONEST_UA)

    assert main(["refresh-podcasts", "--limit", "0"]) == 1

    assert "limit" in capsys.readouterr().err


def test_refresh_with_an_invalid_backoff_fails_naming_the_variable(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", SECRET_URL)
    monkeypatch.setenv("DDM_USER_AGENT", HONEST_UA)
    monkeypatch.setenv("DDM_REFUSED_BACKOFF_DAYS", "0")

    assert main(["refresh-podcasts"]) == 1

    err = capsys.readouterr().err
    assert "DDM_REFUSED_BACKOFF_DAYS" in err
    assert "hunter2" not in err


def test_refresh_passes_the_configured_backoff_to_the_fetcher(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", SECRET_URL)
    monkeypatch.setenv("DDM_USER_AGENT", HONEST_UA)
    monkeypatch.setenv("DDM_REFUSED_BACKOFF_DAYS", "3")
    seen = {}

    def fake_fetcher(user_agent, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop before connecting")

    monkeypatch.setattr("ddm.cli.FeedFetcher", fake_fetcher)

    with pytest.raises(RuntimeError, match="stop before connecting"):
        main(["refresh-podcasts"])

    assert seen == {"refused_backoff": timedelta(days=3)}


@pytest.mark.integration
def test_refresh_twice_reports_counts_and_no_new_observations(
    monkeypatch, capsys, fresh_db, pg_url, tmp_path
):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("DDM_USER_AGENT", HONEST_UA)
    feeds = {f"https://feeds.example.com/{k}.xml": rss(f"Show {k}") for k in (1, 2)}
    monkeypatch.setattr("ddm.cli.FeedFetcher", lambda user_agent, **_: FakeFetcher(feeds))
    assert main(["migrate"]) == 0
    assert (
        main(["import-baseline", "--dump", str(make_dump(tmp_path, [dump_row(1), dump_row(2)]))])
        == 0
    )
    capsys.readouterr()

    assert main(["refresh-podcasts", "--limit", "5"]) == 0
    first = capsys.readouterr().out
    run_id = first.split()[0].removeprefix("run_id=")
    assert main(["refresh-podcasts", "--same-as", run_id]) == 0
    second = capsys.readouterr().out

    assert "status=ok targets=2 fetched=2 not_modified=0 rejected=0 failed=0 skipped=0" in first
    assert "new=2 changed=0 unchanged=0" in first
    assert "activity active=2" in first
    assert "new=0 changed=0 unchanged=2" in second
    assert "ddm-local-only" not in first + second
