"""Command line entry point: `ddm migrate` and `ddm import-baseline`.

Jobs use the direct (non-pooled) DATABASE_URL: temp tables and advisory locks do not
survive PgBouncer transaction pooling. Output names the database host at most, never
the URL or credentials.
"""

import argparse
import logging
import sqlite3
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import psycopg

from ddm.config import NEON_POOLER_SUFFIX, ConfigError, load_database_url
from ddm.db.migrate import MigrationError, discover, migrate
from ddm.ingest.baseline import BASELINE_CUTOFF, ImportReport, import_baseline

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 15
BYTES_PER_MB = 1024 * 1024


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
    return psycopg.connect(url, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS)


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


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        url = load_database_url()
        if args.command == "migrate":
            _run_migrate(url)
        else:
            _run_import_baseline(url, args.dump, args.cutoff)
    except (ConfigError, MigrationError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (psycopg.Error, sqlite3.Error) as exc:
        # Driver messages can echo parts of the connection string; print the class only.
        print(f"error: database operation failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
