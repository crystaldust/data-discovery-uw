import time
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
    assert "schema up to date version=5" in out
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


def test_discover_dump_parses_its_arguments():
    args = build_parser().parse_args(["discover-dump", "--dump", "x.db"])

    assert (args.dump.name, args.created_from) == ("x.db", date(2024, 1, 1))


@pytest.mark.integration
def test_discover_dump_without_a_baseline_fails_cleanly(
    monkeypatch, capsys, fresh_db, pg_url, tmp_path
):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    assert main(["migrate"]) == 0

    assert main(["discover-dump", "--dump", str(make_dump(tmp_path, [dump_row(1)]))]) == 1

    assert "baseline" in capsys.readouterr().err


@pytest.mark.integration
def test_discover_dump_twice_reports_classes_then_skips(
    monkeypatch, capsys, fresh_db, pg_url, tmp_path
):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    moved = dump_row(101, podcastGuid="g1", title="Show 1", createdOn=1710000000)
    rows = [dump_row(1, podcastGuid="g1"), moved]
    path = str(make_dump(tmp_path, rows))
    assert main(["migrate"]) == 0
    assert main(["import-baseline", "--dump", path]) == 0
    capsys.readouterr()

    assert main(["discover-dump", "--dump", path]) == 0
    first = capsys.readouterr().out
    assert main(["discover-dump", "--dump", path]) == 0
    second = capsys.readouterr().out

    assert "status=ok candidates=1 rejected=0" in first
    assert "classified new=0 rename_or_move=1 duplicate=0 uncertain=0" in first
    assert "already discovered (same input fingerprint), nothing written" in second
    assert "ddm-local-only" not in first + second


def test_discover_newfeeds_parses_its_arguments():
    assert build_parser().parse_args(["discover-newfeeds"]).limit == 2000
    assert build_parser().parse_args(["discover-newfeeds", "--limit", "5"]).limit == 5


def test_discover_newfeeds_without_api_keys_fails_naming_the_variable(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", SECRET_URL)
    monkeypatch.setenv("DDM_USER_AGENT", HONEST_UA)
    monkeypatch.delenv("PODCASTINDEX_API_KEY", raising=False)

    assert main(["discover-newfeeds"]) == 1

    err = capsys.readouterr().err
    assert "PODCASTINDEX_API_KEY" in err
    assert "hunter2" not in err


def test_discover_newfeeds_limit_out_of_range_fails_before_connecting(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", SECRET_URL)
    monkeypatch.setenv("DDM_USER_AGENT", HONEST_UA)
    monkeypatch.setenv("PODCASTINDEX_API_KEY", "k")
    monkeypatch.setenv("PODCASTINDEX_API_SECRET", "s")

    assert main(["discover-newfeeds", "--limit", "0"]) == 1

    assert "limit" in capsys.readouterr().err


@pytest.mark.integration
def test_discover_newfeeds_reports_classes_and_the_cursor(
    monkeypatch, capsys, fresh_db, pg_url, tmp_path
):
    from tests.ingest.test_discover_newfeeds import FakeApi, api_feed, listing

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("DDM_USER_AGENT", HONEST_UA)
    monkeypatch.setenv("PODCASTINDEX_API_KEY", "k")
    monkeypatch.setenv("PODCASTINDEX_API_SECRET", "s")
    three_days_ago = int(time.time()) - 3 * 86400  # the CLI uses the real clock
    api = FakeApi([listing(100, added=three_days_ago)], {100: api_feed(100)})
    monkeypatch.setattr("ddm.cli.PodcastIndexClient", lambda config: _Closing(api))
    assert main(["migrate"]) == 0
    assert main(["import-baseline", "--dump", str(make_dump(tmp_path, [dump_row(1)]))]) == 0
    capsys.readouterr()

    assert main(["discover-newfeeds"]) == 0

    out = capsys.readouterr().out
    assert "status=ok stopped=caught_up cursor=100" in out
    assert "classified new=1 rename_or_move=0 duplicate=0 uncertain=0" in out


class _Closing:
    """Wraps a fake API in the client's context-manager interface."""

    def __init__(self, api):
        self.api = api

    def __enter__(self):
        return self.api

    def __exit__(self, *exc_info):
        return None


def test_eval_report_needs_no_database(monkeypatch, tmp_path):
    from tests.eval.test_report import POPULATIONS, sample_rows, write

    monkeypatch.delenv("DATABASE_URL", raising=False)
    labels, strata = write(tmp_path, sample_rows(), POPULATIONS)
    out = tmp_path / "report.md"

    code = main(
        ["eval-report", "--labels", str(labels), "--strata", str(strata), "--out", str(out)]
    )

    assert code == 0
    assert out.read_text(encoding="utf-8").startswith("# Discovery accuracy evaluation")


def test_eval_report_with_a_bad_label_fails_with_the_line(monkeypatch, capsys, tmp_path):
    from tests.eval.test_report import POPULATIONS, sample_rows, write

    rows = sample_rows()
    rows[0] = {**rows[0], "label": "maybe"}
    labels, strata = write(tmp_path, rows, POPULATIONS)

    code = main(
        [
            "eval-report",
            "--labels",
            str(labels),
            "--strata",
            str(strata),
            "--out",
            str(tmp_path / "r.md"),
        ]
    )

    assert code == 1
    assert "line 2" in capsys.readouterr().err


@pytest.mark.integration
def test_eval_sample_writes_the_pairs_to_label(monkeypatch, capsys, fresh_db, pg_url, tmp_path):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    rows = [dump_row(1, podcastGuid="g1"), dump_row(101, podcastGuid="g1", createdOn=1710000000)]
    path = str(make_dump(tmp_path, rows))
    for argv in (
        ["migrate"],
        ["import-baseline", "--dump", path],
        ["discover-dump", "--dump", path],
    ):
        assert main(argv) == 0
    capsys.readouterr()
    labels, strata = tmp_path / "labels.csv", tmp_path / "strata.json"

    assert main(["eval-sample", "--out", str(labels), "--strata", str(strata)]) == 0

    assert "pairs=1" in capsys.readouterr().out
    assert labels.exists() and strata.exists()


@pytest.mark.integration
def test_a_refused_newfeeds_run_exits_with_the_partial_code(
    monkeypatch, capsys, fresh_db, pg_url, tmp_path
):
    from tests.ingest.test_discover_newfeeds import FakeApi, listing

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("DDM_USER_AGENT", HONEST_UA)
    monkeypatch.setenv("PODCASTINDEX_API_KEY", "k")
    monkeypatch.setenv("PODCASTINDEX_API_SECRET", "s")
    three_days_ago = int(time.time()) - 3 * 86400
    api = FakeApi([listing(100, added=three_days_ago)], {}, refuse={100})
    monkeypatch.setattr("ddm.cli.PodcastIndexClient", lambda config: _Closing(api))
    assert main(["migrate"]) == 0
    assert main(["import-baseline", "--dump", str(make_dump(tmp_path, [dump_row(1)]))]) == 0
    capsys.readouterr()

    assert main(["discover-newfeeds"]) == 3
    assert "status=partial stopped=refused" in capsys.readouterr().out

    assert main(["discover-newfeeds"]) == 1
    assert "not asking again before" in capsys.readouterr().err


def test_eval_report_refuses_to_write_over_its_inputs(tmp_path, capsys):
    from tests.eval.test_report import POPULATIONS, sample_rows, write

    labels, strata = write(tmp_path, sample_rows(), POPULATIONS)
    before = labels.read_bytes()

    code = main(
        ["eval-report", "--labels", str(labels), "--strata", str(strata), "--out", str(labels)]
    )

    assert code == 1
    assert labels.read_bytes() == before
    assert "would overwrite" in capsys.readouterr().err


class _FakeLabelServer:
    url = "http://127.0.0.1:8765/?token=abc"

    def __init__(self):
        self.closed = False

    def serve_forever(self):
        raise KeyboardInterrupt

    def server_close(self):
        self.closed = True


def test_label_starts_the_local_page_and_stops_cleanly_on_ctrl_c(monkeypatch, capsys, tmp_path):
    from tests.eval.test_labeling import rows, write_csv

    labels = write_csv(tmp_path / "labels.csv", rows())
    fake, opened, seen = _FakeLabelServer(), [], {}

    def make(path, *, port):
        seen.update(path=path, port=port)
        return fake

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr("ddm.cli.make_server", make)
    monkeypatch.setattr("ddm.cli.webbrowser.open", opened.append)

    assert main(["label", "--labels", str(labels), "--port", "9000"]) == 0

    assert seen == {"path": labels, "port": 9000}
    assert opened == [fake.url]
    assert fake.url in capsys.readouterr().out
    assert fake.closed


def test_label_can_leave_the_browser_alone(monkeypatch, tmp_path):
    from tests.eval.test_labeling import rows, write_csv

    labels = write_csv(tmp_path / "labels.csv", rows())
    opened = []
    monkeypatch.setattr("ddm.cli.make_server", lambda path, *, port: _FakeLabelServer())
    monkeypatch.setattr("ddm.cli.webbrowser.open", opened.append)

    assert main(["label", "--labels", str(labels), "--no-browser"]) == 0
    assert opened == []


def test_label_without_the_file_fails_cleanly(capsys, tmp_path):
    assert main(["label", "--labels", str(tmp_path / "missing.csv")]) == 1
    assert "missing.csv" in capsys.readouterr().err


def test_label_on_a_busy_port_says_so(monkeypatch, capsys, tmp_path):
    from tests.eval.test_labeling import rows, write_csv

    labels = write_csv(tmp_path / "labels.csv", rows())

    def busy(path, *, port):
        raise OSError(48, "Address already in use")

    monkeypatch.setattr("ddm.cli.make_server", busy)

    assert main(["label", "--labels", str(labels), "--port", "9000"]) == 1
    assert "--port" in capsys.readouterr().err


def test_sync_journalists_without_a_user_agent_fails_naming_the_variable(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", SECRET_URL)
    monkeypatch.delenv("DDM_USER_AGENT", raising=False)

    assert main(["sync-journalists"]) == 1

    err = capsys.readouterr().err
    assert "DDM_USER_AGENT" in err
    assert "hunter2" not in err


@pytest.mark.integration
def test_sync_journalists_reports_the_snapshot(monkeypatch, capsys, fresh_db, pg_url):
    from tests.ingest.test_wikidata_sync import ATLANTIC_PEOPLE, DETAILS, FakeClient

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("DDM_USER_AGENT", HONEST_UA)
    client = FakeClient(ATLANTIC_PEOPLE, DETAILS)
    monkeypatch.setattr("ddm.cli.WikidataClient", lambda user_agent: _Closing(client))
    assert main(["migrate"]) == 0
    capsys.readouterr()

    assert main(["sync-journalists"]) == 0

    out = capsys.readouterr().out
    assert "status=ok provenance=historical" in out
    assert "new=2 changed=0 unchanged=0 deleted=0" in out
    assert "sparql_requests=21" in out


@pytest.mark.integration
def test_a_refused_journalist_sync_exits_with_the_partial_code(
    monkeypatch, capsys, fresh_db, pg_url
):
    from tests.ingest.test_wikidata_sync import ATLANTIC_PEOPLE, FakeClient

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("DDM_USER_AGENT", HONEST_UA)
    client = FakeClient(ATLANTIC_PEOPLE, refuse_at=1)
    monkeypatch.setattr("ddm.cli.WikidataClient", lambda user_agent: _Closing(client))
    assert main(["migrate"]) == 0

    assert main(["sync-journalists"]) == 3
    assert "stopped=refused" in capsys.readouterr().out
    assert main(["sync-journalists"]) == 1
    assert "refused an earlier run" in capsys.readouterr().err


def test_sync_bylines_parses_its_arguments():
    assert build_parser().parse_args(["sync-bylines"]).author_feeds == 50
    assert build_parser().parse_args(["sync-bylines", "--author-feeds", "5"]).author_feeds == 5


def test_sync_bylines_author_feeds_out_of_range_fails_before_connecting(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", SECRET_URL)
    monkeypatch.setenv("DDM_USER_AGENT", HONEST_UA)

    assert main(["sync-bylines", "--author-feeds", "201"]) == 1

    assert "--author-feeds" in capsys.readouterr().err


@pytest.mark.integration
def test_sync_bylines_reports_classes_and_outcomes(monkeypatch, capsys, fresh_db, pg_url):
    from tests.ingest.test_bylines import BODIES, DETAILS, PEOPLE, fetcher
    from tests.ingest.test_wikidata_sync import FakeClient

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("DDM_USER_AGENT", HONEST_UA)
    client = FakeClient(PEOPLE, DETAILS)
    monkeypatch.setattr("ddm.cli.WikidataClient", lambda user_agent: _Closing(client))
    feeds = fetcher(BODIES)
    monkeypatch.setattr("ddm.cli.FeedFetcher", lambda *a, **k: feeds)
    assert main(["migrate"]) == 0
    assert main(["sync-journalists"]) == 0
    capsys.readouterr()

    assert main(["sync-bylines"]) == 0

    out = capsys.readouterr().out
    assert "status=ok feeds=6 fetched=6" in out
    assert "authors=5 organisation_bylines=1" in out
    assert "classified new=2 rename_or_move=1 duplicate=1 uncertain=1" in out


def test_sync_bylines_hides_author_slugs_in_fetch_logs(monkeypatch):
    from ddm.sources.outlet_rss import public_url

    monkeypatch.setenv("DATABASE_URL", SECRET_URL)
    monkeypatch.setenv("DDM_USER_AGENT", HONEST_UA)
    seen = {}

    def fake_fetcher(user_agent, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop before connecting")

    monkeypatch.setattr("ddm.cli.FeedFetcher", fake_fetcher)

    with pytest.raises(RuntimeError, match="stop before connecting"):
        main(["sync-bylines"])

    assert seen["log_url"] is public_url
