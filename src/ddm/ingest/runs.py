"""Import run registration, status, metrics and rejects.

A run is keyed by (source, input fingerprint), so the same input is registered once.
Registration commits on its own; data for the run is written in a separate transaction,
so a failure leaves the run 'running' and the next attempt resumes the same run_id.
Connections are expected in autocommit mode with explicit conn.transaction() blocks.
"""

from collections.abc import Mapping, Sequence
from dataclasses import astuple, dataclass, fields
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ddm.ingest.records import Reject, canonical_json

# One importer at a time across all sources: entities and identifiers are shared tables.
IMPORT_LOCK_KEY = 4_242_100
RUN_STATUSES = frozenset({"running", "ok", "partial", "failed"})
FINISHED_STATUSES = frozenset({"ok", "partial"})


@dataclass(frozen=True)
class RunHandle:
    run_id: int
    status: str
    resumed: bool


@dataclass(frozen=True)
class RunMetrics:
    """Column-for-column copy of run_metrics; None means not measured."""

    rows_in: int | None = None
    rows_rejected: int | None = None
    rows_new: int | None = None
    rows_changed: int | None = None
    rows_unchanged: int | None = None
    entities_created: int | None = None
    identifiers_added: int | None = None
    strong_id_merges: int | None = None
    candidates_created: int | None = None
    ambiguous_identifiers: int | None = None
    http_requests: int | None = None
    bytes_fetched: int | None = None
    duration_ms: int | None = None
    db_size_bytes: int | None = None
    rows_not_modified: int | None = None
    rows_failed: int | None = None
    rows_skipped: int | None = None


METRIC_COLUMNS = tuple(f.name for f in fields(RunMetrics))


def _sum(a: int | None, b: int | None) -> int | None:
    if a is None:
        return b
    return a if b is None else a + b


def add_metrics(a: RunMetrics, b: RunMetrics) -> RunMetrics:
    """Column-wise sum as a new object; a column stays None only if both are unmeasured."""
    return RunMetrics(*(_sum(getattr(a, name), getattr(b, name)) for name in METRIC_COLUMNS))


def source_id_of(conn: psycopg.Connection, name: str) -> int:
    row = conn.execute("SELECT source_id FROM sources WHERE name = %s", (name,)).fetchone()
    if row is None:
        raise RuntimeError(f"source {name} is not registered; run migrations first")
    return row[0]


def register_run(
    conn: psycopg.Connection,
    *,
    source_id: int,
    run_kind: str,
    fingerprint: str,
    is_complete_snapshot: bool,
    params: Mapping[str, Any],
) -> RunHandle:
    with conn.transaction():
        inserted = conn.execute(
            """
            INSERT INTO import_runs
              (source_id, run_kind, input_fingerprint, is_complete_snapshot, params)
            VALUES (%s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (source_id, input_fingerprint) DO NOTHING
            RETURNING run_id
            """,
            (source_id, run_kind, fingerprint, is_complete_snapshot, canonical_json(params)),
        ).fetchone()
        if inserted:
            return RunHandle(run_id=inserted[0], status="running", resumed=False)
        run_id, status = conn.execute(
            "SELECT run_id, status FROM import_runs"
            " WHERE source_id = %s AND input_fingerprint = %s",
            (source_id, fingerprint),
        ).fetchone()
        if status in FINISHED_STATUSES:
            return RunHandle(run_id=run_id, status=status, resumed=False)
        conn.execute(
            "UPDATE import_runs SET status = 'running', finished_at = NULL WHERE run_id = %s",
            (run_id,),
        )
        return RunHandle(run_id=run_id, status="running", resumed=True)


def finish_run(conn: psycopg.Connection, run_id: int, status: str) -> None:
    if status not in RUN_STATUSES:
        raise ValueError(f"unknown run status {status}")
    with conn.transaction():
        conn.execute(
            "UPDATE import_runs SET status = %s, finished_at = now() WHERE run_id = %s",
            (status, run_id),
        )


def write_metrics(conn: psycopg.Connection, run_id: int, metrics: RunMetrics) -> None:
    columns = ", ".join(METRIC_COLUMNS)
    placeholders = ", ".join(["%s"] * len(METRIC_COLUMNS))
    updates = ", ".join(f"{name} = excluded.{name}" for name in METRIC_COLUMNS)
    # Column names come from the RunMetrics dataclass, never from input.
    conn.execute(
        f"""
        INSERT INTO run_metrics (run_id, {columns}) VALUES (%s, {placeholders})
        ON CONFLICT (run_id) DO UPDATE SET {updates}
        """,  # noqa: S608
        (run_id, *astuple(metrics)),
    )


def read_metrics(conn: psycopg.Connection, run_id: int) -> RunMetrics | None:
    """The run's metrics, or None when its data has not been committed yet."""
    row = conn.execute(
        f"SELECT {', '.join(METRIC_COLUMNS)} FROM run_metrics WHERE run_id = %s",  # noqa: S608
        (run_id,),
    ).fetchone()
    return RunMetrics(*row) if row else None


def write_rejects(conn: psycopg.Connection, run_id: int, rejects: Sequence[Reject]) -> int:
    """Replace this run's rejects (an interrupted attempt may have left some)."""
    conn.execute("DELETE FROM import_rejects WHERE run_id = %s", (run_id,))
    return append_rejects(conn, run_id, rejects)


def append_rejects(conn: psycopg.Connection, run_id: int, rejects: Sequence[Reject]) -> int:
    """Add rejects for a run written in batches; row numbers must be unique across batches."""
    with (
        conn.cursor() as cur,
        cur.copy(
            "COPY import_rejects (run_id, row_number, source_key, reason, raw) FROM STDIN"
        ) as copy,
    ):
        copy.set_types(["int8", "int4", "text", "text", "jsonb"])
        for reject in rejects:
            copy.write_row(
                (
                    run_id,
                    reject.row_number,
                    reject.source_key,
                    reject.reason,
                    Jsonb(dict(reject.raw)),
                )
            )
    return len(rejects)
