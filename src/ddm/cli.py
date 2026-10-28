"""Command line entry point: `ddm migrate`, `ddm import-baseline`, `ddm refresh-podcasts`,
discovery, evaluation and the journalist jobs.

Jobs use the direct (non-pooled) DATABASE_URL: temp tables and advisory locks do not
survive PgBouncer transaction pooling. Output names the database host at most, never
the URL or credentials.
"""

import argparse
import logging
import sqlite3
import sys
import webbrowser
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import psycopg

from ddm.config import (
    NEON_POOLER_SUFFIX,
    ConfigError,
    load_database_url,
    load_podcastindex_config,
    load_refused_backoff,
    load_user_agent,
)
from ddm.db.migrate import MigrationError, discover, migrate
from ddm.eval.label_server import DEFAULT_PORT as LABEL_PORT
from ddm.eval.label_server import make_server
from ddm.eval.report import write_report
from ddm.eval.sample import EvalInputError, write_sample
from ddm.ingest.backoff import DiscoveryBackoff
from ddm.ingest.baseline import BASELINE_CUTOFF, ImportReport, import_baseline
from ddm.ingest.bylines import (
    DEFAULT_AUTHOR_FEEDS,
    MAX_AUTHOR_FEEDS,
    BylineInputError,
    BylineReport,
    sync_bylines,
)
from ddm.ingest.discover_dump import DiscoveryInputError, DiscoveryReport, discover_dump
from ddm.ingest.discover_newfeeds import DEFAULT_LIMIT as NEWFEEDS_LIMIT
from ddm.ingest.discover_newfeeds import MAX_LIMIT as NEWFEEDS_MAX
from ddm.ingest.discover_newfeeds import DiscoveryBusy, NewfeedsReport, discover_newfeeds
from ddm.ingest.fetch_log import RefreshInputError
from ddm.ingest.refresh import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    RefreshBusy,
    RefreshReport,
    refresh_podcasts,
)
from ddm.ingest.wikidata_sync import (
    JournalistInputError,
    JournalistSyncBusy,
    JournalistSyncReport,
    sync_journalists,
)
from ddm.sources.outlet_rss import public_url
from ddm.sources.podcastindex import PodcastIndexClient
from ddm.sources.rss_fetch import REFUSED_BACKOFF, FeedFetcher
from ddm.sources.wikidata import WikidataClient

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 15
KEEPALIVE_IDLE_SECONDS = 30
BYTES_PER_MB = 1024 * 1024
OUTCOMES = ("fetched", "not_modified", "rejected", "failed", "skipped")
ACTIVITY = ("active", "dormant", "dead", "unknown")
EVAL_DIR = Path("eval")
# A run that stopped early (refused or source down): not a failure, not a success either.
PARTIAL_EXIT = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ddm", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="apply pending SQL migrations")
    baseline = commands.add_parser(
        "import-baseline", help="import the historical podcast baseline from the dump"
    )
    baseline.add_argument("--dump", type=Path, required=True, help="podcastindex_feeds.db path")
    baseline.add_argument(
        "--cutoff",
        type=date.fromisoformat,
        default=BASELINE_CUTOFF,
        help="feeds created before this date (YYYY-MM-DD) form the baseline",
    )
    refresh = commands.add_parser(
        "refresh-podcasts", help="re-fetch baseline RSS feeds, least recently checked first"
    )
    refresh.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT, help=f"feeds to fetch (1-{MAX_LIMIT})"
    )
    refresh.add_argument(
        "--same-as", type=int, help="re-fetch exactly the feeds of this earlier refresh run"
    )
    discover = commands.add_parser(
        "discover-dump", help="classify feeds the dump added since the baseline cutoff"
    )
    discover.add_argument("--dump", type=Path, required=True, help="podcastindex_feeds.db path")
    discover.add_argument(
        "--created-from",
        type=date.fromisoformat,
        default=BASELINE_CUTOFF,
        help="feeds created on or after this date (YYYY-MM-DD) are candidates",
    )
    newfeeds = commands.add_parser(
        "discover-newfeeds", help="classify feeds Podcast Index added since the last run"
    )
    newfeeds.add_argument(
        "--limit",
        type=int,
        default=NEWFEEDS_LIMIT,
        help=f"English feeds to look up (1-{NEWFEEDS_MAX})",
    )
    commands.add_parser("sync-journalists", help="snapshot the outlets' journalists from Wikidata")
    bylines = commands.add_parser(
        "sync-bylines", help="read outlet RSS bylines and classify first-seen authors"
    )
    bylines.add_argument(
        "--author-feeds",
        type=int,
        default=DEFAULT_AUTHOR_FEEDS,
        help=f"per-author feeds to read besides the section feeds (1-{MAX_AUTHOR_FEEDS})",
    )
    sample = commands.add_parser("eval-sample", help="draw the pairs to label by hand")
    sample.add_argument("--out", type=Path, default=EVAL_DIR / "labels.csv")
    sample.add_argument("--strata", type=Path, default=EVAL_DIR / "strata.json")
    sample.add_argument(
        "--carry-from",
        type=Path,
        help="keep labels of pairs found in this earlier file (move it away from --out first)",
    )
    report = commands.add_parser(
        "eval-report", help="build the accuracy report from the labels (no database needed)"
    )
    report.add_argument("--labels", type=Path, default=EVAL_DIR / "labels.csv")
    report.add_argument("--strata", type=Path, default=EVAL_DIR / "strata.json")
    report.add_argument("--out", type=Path, default=EVAL_DIR / "report.md")
    label = commands.add_parser(
        "label", help="open a local page to compare and label the sampled pairs"
    )
    label.add_argument("--labels", type=Path, default=EVAL_DIR / "labels.csv")
    label.add_argument("--port", type=int, default=LABEL_PORT, help="local port (127.0.0.1)")
    label.add_argument("--no-browser", action="store_true", help="only print the address")
    return parser


def _host(url: str) -> str:
    try:
        return urlsplit(url).hostname or "unknown"
    except ValueError:
        return "unknown"


def _connect(url: str) -> psycopg.Connection:
    host = _host(url)
    if NEON_POOLER_SUFFIX in host.partition(".")[0]:
        raise ConfigError("DATABASE_URL must be the direct (non-pooled) connection for jobs")
    logger.info("connecting host=%s", host)
    # Keepalives: a refresh leaves the connection idle while it fetches a batch.
    return psycopg.connect(
        url,
        autocommit=True,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        keepalives=1,
        keepalives_idle=KEEPALIVE_IDLE_SECONDS,
    )


def _run_migrate(url: str) -> None:
    with _connect(url) as conn:
        applied = migrate(conn)
    for migration in applied:
        print(f"applied version={migration.version} name={migration.name}")
    latest = max((m.version for m in discover()), default=0)
    print(f"schema up to date version={latest}")


def _format_report(report: ImportReport) -> str:
    if report.skipped:
        return (
            f"run_id={report.run_id} status={report.status} "
            "already imported (same input fingerprint), nothing written"
        )
    m = report.metrics
    size_mb = (m.db_size_bytes or 0) / BYTES_PER_MB
    return (
        f"run_id={report.run_id} status={report.status} rows_in={m.rows_in} "
        f"rejected={m.rows_rejected} new={m.rows_new} changed={m.rows_changed} "
        f"unchanged={m.rows_unchanged}\n"
        f"entities_created={m.entities_created} identifiers_added={m.identifiers_added} "
        f"strong_id_merges={m.strong_id_merges} candidates={m.candidates_created}\n"
        f"db_size={size_mb:.1f} MB duration={(m.duration_ms or 0) / 1000:.1f} s"
    )


def _run_import_baseline(url: str, dump: Path, cutoff: date) -> None:
    if not dump.is_file():
        raise FileNotFoundError(f"dump not found: {dump}")
    with _connect(url) as conn:
        report = import_baseline(conn, dump, cutoff)
    print(_format_report(report))


def _format_discovery(report: DiscoveryReport) -> str:
    if report.skipped:
        return (
            f"run_id={report.run_id} status={report.status} "
            "already discovered (same input fingerprint), nothing written"
        )
    m = report.metrics
    return (
        f"run_id={report.run_id} status={report.status} candidates={m.rows_in} "
        f"rejected={m.rows_rejected}\n"
        f"{_classified(m)}\n"
        f"entities_created={m.entities_created} strong_id_merges={m.strong_id_merges} "
        f"candidates_queued={m.candidates_created} "
        f"ambiguous_identifiers={m.ambiguous_identifiers}\n"
        f"db_size={(m.db_size_bytes or 0) / BYTES_PER_MB:.1f} MB "
        f"duration={(m.duration_ms or 0) / 1000:.1f} s"
    )


def _run_discover_dump(url: str, dump: Path, created_from: date) -> None:
    if not dump.is_file():
        raise FileNotFoundError(f"dump not found: {dump}")
    with _connect(url) as conn:
        report = discover_dump(conn, dump, created_from)
    print(_format_discovery(report))


def _classified(m) -> str:
    return (
        f"classified new={m.discovered_new} rename_or_move={m.discovered_rename_or_move} "
        f"duplicate={m.discovered_duplicate} uncertain={m.discovered_uncertain}"
    )


def _format_newfeeds(report: NewfeedsReport) -> str:
    m = report.metrics
    return (
        f"run_id={report.run_id} status={report.status} stopped={report.stopped} "
        f"cursor={report.cursor}\n"
        f"looked_up={m.rows_in} skipped={m.rows_skipped} rejected={m.rows_rejected} "
        f"api_requests={m.http_requests}\n"
        f"{_classified(m)}\n"
        f"db_size={(m.db_size_bytes or 0) / BYTES_PER_MB:.1f} MB "
        f"duration={(m.duration_ms or 0) / 1000:.1f} s"
    )


def _run_discover_newfeeds(url: str, limit: int) -> int:
    config = load_podcastindex_config()
    refused_backoff = load_refused_backoff(REFUSED_BACKOFF)
    if not 1 <= limit <= NEWFEEDS_MAX:
        raise DiscoveryInputError(f"--limit must be between 1 and {NEWFEEDS_MAX}")
    with PodcastIndexClient(config) as client, _connect(url) as conn:
        report = discover_newfeeds(conn, client, limit=limit, refused_backoff=refused_backoff)
    print(_format_newfeeds(report))
    return 0 if report.status == "ok" else PARTIAL_EXIT


def _format_journalists(report: JournalistSyncReport) -> str:
    m = report.metrics
    return (
        f"run_id={report.run_id} status={report.status} provenance={report.provenance} "
        f"stopped={report.stopped or '-'} rejected={m.rows_rejected or 0}\n"
        f"new={m.rows_new or 0} changed={m.rows_changed or 0} "
        f"unchanged={m.rows_unchanged or 0} deleted={m.rows_deleted or 0} "
        f"deletions_withheld={report.deletions_withheld}\n"
        f"entities_created={m.entities_created or 0} strong_id_merges={m.strong_id_merges or 0} "
        f"candidates={m.candidates_created or 0} identifiers_added={m.identifiers_added or 0}\n"
        f"sparql_requests={m.http_requests or 0} "
        f"db_size={(m.db_size_bytes or 0) / BYTES_PER_MB:.1f} MB "
        f"duration={(m.duration_ms or 0) / 1000:.1f} s"
    )


def _run_sync_journalists(url: str) -> int:
    user_agent = load_user_agent()
    refused_backoff = load_refused_backoff(REFUSED_BACKOFF)
    with WikidataClient(user_agent) as client, _connect(url) as conn:
        report = sync_journalists(conn, client, refused_backoff=refused_backoff)
    print(_format_journalists(report))
    return 0 if report.status == "ok" else PARTIAL_EXIT


def _format_bylines(report: BylineReport) -> str:
    m = report.metrics
    return (
        f"run_id={report.run_id} status={report.status} feeds={sum(report.outcomes.values())} "
        f"{_counts('', OUTCOMES, report.outcomes)}\n"
        f"bylines={report.bylines} authors={report.authors} "
        f"organisation_bylines={report.organisation_bylines}\n"
        f"{_classified(m)}\n"
        f"http_requests={m.http_requests or 0} "
        f"db_size={(m.db_size_bytes or 0) / BYTES_PER_MB:.1f} MB "
        f"duration={(m.duration_ms or 0) / 1000:.1f} s"
    )


def _run_sync_bylines(url: str, author_feeds: int) -> int:
    user_agent = load_user_agent()
    refused_backoff = load_refused_backoff(REFUSED_BACKOFF)
    if not 1 <= author_feeds <= MAX_AUTHOR_FEEDS:
        raise BylineInputError(f"--author-feeds must be between 1 and {MAX_AUTHOR_FEEDS}")
    with (
        FeedFetcher(user_agent, refused_backoff=refused_backoff, log_url=public_url) as fetcher,
        _connect(url) as conn,
    ):
        report = sync_bylines(conn, fetcher, author_feeds=author_feeds)
    print(_format_bylines(report))
    return 0 if report.status == "ok" else PARTIAL_EXIT


def _run_eval_sample(url: str, out: Path, strata: Path, carry_from: Path | None) -> None:
    with _connect(url) as conn:
        result = write_sample(conn, out, strata, carry_from)
    print(
        f"pairs={result.pairs} carried_labels={result.carried} "
        f"dropped_invalid_labels={result.dropped} labels={out} strata={strata}"
    )


def _run_eval_report(labels: Path, strata: Path, out: Path) -> None:
    for path in (labels, strata):
        if not path.is_file():
            raise FileNotFoundError(f"not found: {path}")
    if out.resolve() in (labels.resolve(), strata.resolve()):
        raise EvalInputError(f"--out {out} would overwrite an input of the report")
    write_report(labels, strata, out)
    print(f"report={out}")


def _run_label(labels: Path, port: int, open_browser: bool) -> None:
    if not labels.is_file():
        raise FileNotFoundError(f"not found: {labels}")
    try:
        server = make_server(labels, port=port)
    except OSError as exc:
        raise EvalInputError(f"cannot listen on 127.0.0.1:{port}; choose another --port") from exc
    # Flushed: serve_forever blocks, and a piped stdout would otherwise never show the address.
    print(
        f"labelling page: {server.url}\nlabels are saved to {labels}; press Ctrl-C to stop",
        flush=True,
    )
    if open_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
    finally:
        server.server_close()


def _counts(prefix: str, names: Sequence[str], counts) -> str:
    return prefix + " ".join(f"{name}={counts.get(name, 0)}" for name in names)


def _format_refresh(report: RefreshReport) -> str:
    m = report.metrics
    return (
        f"run_id={report.run_id} status={report.status} targets={m.rows_in} "
        f"{_counts('', OUTCOMES, report.outcomes)}\n"
        f"new={m.rows_new} changed={m.rows_changed} unchanged={m.rows_unchanged} "
        f"identifiers_added={m.identifiers_added} candidates={m.candidates_created}\n"
        f"{_counts('activity ', ACTIVITY, report.activity)}\n"
        f"http_requests={m.http_requests} fetched_mb={(m.bytes_fetched or 0) / BYTES_PER_MB:.1f} "
        f"db_size={(m.db_size_bytes or 0) / BYTES_PER_MB:.1f} MB "
        f"duration={(m.duration_ms or 0) / 1000:.1f} s"
    )


def _run_refresh(url: str, limit: int, same_as: int | None) -> None:
    user_agent = load_user_agent()
    refused_backoff = load_refused_backoff(REFUSED_BACKOFF)
    if not 1 <= limit <= MAX_LIMIT:
        raise RefreshInputError(f"--limit must be between 1 and {MAX_LIMIT}")
    with (
        FeedFetcher(user_agent, refused_backoff=refused_backoff) as fetcher,
        _connect(url) as conn,
    ):
        report = refresh_podcasts(conn, fetcher, limit=limit, same_as=same_as)
    print(_format_refresh(report))


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        if args.command == "eval-report":
            # Reproducible by anyone from the two files alone, without a database.
            _run_eval_report(args.labels, args.strata, args.out)
            return 0
        if args.command == "label":
            _run_label(args.labels, args.port, not args.no_browser)
            return 0
        url = load_database_url()
        if args.command == "migrate":
            _run_migrate(url)
        elif args.command == "import-baseline":
            _run_import_baseline(url, args.dump, args.cutoff)
        elif args.command == "discover-dump":
            _run_discover_dump(url, args.dump, args.created_from)
        elif args.command == "discover-newfeeds":
            return _run_discover_newfeeds(url, args.limit)
        elif args.command == "sync-journalists":
            return _run_sync_journalists(url)
        elif args.command == "sync-bylines":
            return _run_sync_bylines(url, args.author_feeds)
        elif args.command == "eval-sample":
            _run_eval_sample(url, args.out, args.strata, args.carry_from)
        else:
            _run_refresh(url, args.limit, args.same_as)
    except (
        ConfigError,
        MigrationError,
        FileNotFoundError,
        RefreshBusy,
        RefreshInputError,
        DiscoveryInputError,
        DiscoveryBusy,
        DiscoveryBackoff,
        EvalInputError,
        JournalistInputError,
        JournalistSyncBusy,
        BylineInputError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (psycopg.Error, sqlite3.Error) as exc:
        # Driver messages can echo parts of the connection string; print the class only.
        print(f"error: database operation failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
