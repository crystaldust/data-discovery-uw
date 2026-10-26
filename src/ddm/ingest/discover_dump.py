"""Whole-library discovery from the Podcast Index export.

Feeds the export added on or after `created_from` (the baseline cutoff by default) are
compared with everything already stored. Flow: check the baseline exists -> read the whole
demo slice once -> normalise (bad rows in the window become rejects) -> fingerprint the
window -> register the run (an already finished input is skipped) -> one transaction under
the import lock for rejects and ambiguous identifiers counted over the whole slice (the
rule the baseline used) -> discover_batch in batches of BATCH_SIZE, each in its own
transaction with cumulative metrics -> ANALYZE -> mark the run ok.

Batches keep the import lock short and survive a dropped connection: a rerun of the same
input resumes the run, skips every key already stored, and adds to the stored metrics.
Classification does not depend on the batch size (see discovery.py).

A window is not a complete snapshot, so nothing is ever inferred deleted from it. A newer
export classifies only the feeds this source has not stored yet.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from datetime import time as day_start
from pathlib import Path
from types import MappingProxyType

import psycopg

from ddm.ingest.classify import CLASSIFIER_VERSION
from ddm.ingest.discovery import DiscoveryCounts, discover_batch
from ddm.ingest.fuzzy import FUZZY_THRESHOLD, SCORER_VERSION
from ddm.ingest.records import (
    NormalizedRecord,
    Reject,
    content_hash,
    fingerprint_item,
    input_fingerprint,
    reject_digest,
)
from ddm.ingest.resolve import (
    RESOLUTION_RULES,
    find_ambiguous_identifiers,
    load_ambiguous_identifiers,
    write_ambiguous_identifiers,
)
from ddm.ingest.runs import (
    IMPORT_LOCK_KEY,
    RunMetrics,
    add_metrics,
    finish_run,
    read_metrics,
    register_run,
    source_id_of,
    write_metrics,
    write_rejects,
)
from ddm.sources.podcastindex_dump import (
    NORMALIZER_VERSION,
    SOURCE_NAME,
    SliceParams,
    iter_slice_rows,
    normalize_dump_row,
)

logger = logging.getLogger(__name__)

DEFAULT_CREATED_FROM = date(2024, 1, 1)
# Far enough ahead to include every feed the export could hold.
SLICE_UNTIL = date(2100, 1, 1)
PROVENANCE = "historical"
# Records per transaction: long enough to amortise the lock, short enough for Neon.
BATCH_SIZE = 2000
ANALYZE_TABLES = (
    "source_records, observations, entities, entity_identifiers, candidate_matches, discoveries"
)


class DiscoveryInputError(ValueError):
    """Discovery cannot run on this input or database state."""


@dataclass(frozen=True)
class DiscoveryReport:
    run_id: int
    status: str
    skipped: bool
    metrics: RunMetrics


@dataclass(frozen=True)
class _Window:
    records: tuple[NormalizedRecord, ...]
    rejects: tuple[Reject, ...]
    whole_slice: tuple[NormalizedRecord, ...]
    params: MappingProxyType
    fingerprint: str


def _epoch(day: date) -> int:
    return int(datetime.combine(day, day_start(), UTC).timestamp())


def _read_window(dump_path: Path, created_from: date) -> _Window:
    start = _epoch(created_from)
    records, rejects, whole = [], [], []
    rows = iter_slice_rows(dump_path, SliceParams(created_before=SLICE_UNTIL))
    for row_number, row in enumerate(rows, start=1):
        result = normalize_dump_row(row_number, row)
        in_window = isinstance(row.get("createdOn"), int) and row["createdOn"] >= start
        if isinstance(result, NormalizedRecord):
            whole.append(result)
            if in_window:
                records.append(result)
        elif in_window:
            rejects.append(result)
    items = [fingerprint_item(r.source_key, content_hash(r)) for r in records] + [
        fingerprint_item(f"reject:{r.row_number}", reject_digest(r)) for r in rejects
    ]
    params = {
        "created_from": created_from,
        "normalizer_version": NORMALIZER_VERSION,
        "classifier_version": CLASSIFIER_VERSION,
        "fuzzy_threshold": FUZZY_THRESHOLD,
        "scorer_version": SCORER_VERSION,
        "resolution_rules": RESOLUTION_RULES,
    }
    return _Window(
        records=tuple(records),
        rejects=tuple(rejects),
        whole_slice=tuple(whole),
        params=MappingProxyType(params),
        fingerprint=input_fingerprint(SOURCE_NAME, params, items),
    )


def _require_baseline(conn: psycopg.Connection, source_id: int) -> None:
    row = conn.execute(
        "SELECT 1 FROM import_runs WHERE source_id = %s AND run_kind = 'historical_backfill'"
        " AND status = 'ok' LIMIT 1",
        (source_id,),
    ).fetchone()
    if row is None:
        raise DiscoveryInputError("import the baseline first (ddm import-baseline)")


def metrics_of(counts: DiscoveryCounts, **extra) -> RunMetrics:
    return RunMetrics(
        rows_new=counts.rows_new,
        rows_changed=counts.rows_changed,
        rows_unchanged=counts.rows_unchanged,
        entities_created=counts.entities_created,
        identifiers_added=counts.identifiers_added,
        strong_id_merges=counts.strong_id_merges,
        candidates_created=counts.candidates_created,
        discovered_new=counts.new,
        discovered_rename_or_move=counts.rename_or_move,
        discovered_duplicate=counts.duplicate,
        discovered_uncertain=counts.uncertain,
        **extra,
    )


def _known_keys(conn: psycopg.Connection, source_id: int, keys: list[str]) -> set[str]:
    rows = conn.execute(
        "SELECT source_key FROM source_records WHERE source_id = %s AND source_key = ANY(%s)",
        (source_id, keys),
    ).fetchall()
    return {key for (key,) in rows}


def _prepare(conn, window: _Window, *, source_id: int, run_id: int) -> RunMetrics:
    """Rejects and ambiguous identifiers, once per run; the counts that go with them."""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (IMPORT_LOCK_KEY,))
        write_rejects(conn, run_id, window.rejects)
        found = find_ambiguous_identifiers(window.whole_slice)
        write_ambiguous_identifiers(conn, found, source_id=source_id, run_id=run_id)
        metrics = metrics_of(
            DiscoveryCounts(),
            rows_in=len(window.records) + len(window.rejects),
            rows_rejected=len(window.rejects),
            ambiguous_identifiers=len(found),
        )
        write_metrics(conn, run_id, metrics)
    return metrics


def _write_batches(conn, records, *, source_id, run_id, batch, size, total) -> RunMetrics:
    for start in range(0, len(records), size):
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (IMPORT_LOCK_KEY,))
            counts = batch(
                conn,
                records[start : start + size],
                source_name=SOURCE_NAME,
                source_id=source_id,
                run_id=run_id,
                provenance=PROVENANCE,
                ambiguous=load_ambiguous_identifiers(conn),
                threshold=FUZZY_THRESHOLD,
            )
            total = add_metrics(total, metrics_of(counts))
            write_metrics(conn, run_id, total)
        logger.info("run_id=%d progress=%d/%d", run_id, start + size, len(records))
    return total


def discover_dump(
    conn: psycopg.Connection,
    dump_path: Path,
    created_from: date = DEFAULT_CREATED_FROM,
    *,
    clock: Callable[[], float] = time.monotonic,
    batch: Callable[..., DiscoveryCounts] = discover_batch,
    batch_size: int = BATCH_SIZE,
) -> DiscoveryReport:
    """Classify the export's feeds added since `created_from`. Expects autocommit."""
    started = clock()
    source_id = source_id_of(conn, SOURCE_NAME)
    _require_baseline(conn, source_id)
    window = _read_window(dump_path, created_from)
    run = register_run(
        conn,
        source_id=source_id,
        run_kind="discovery",
        fingerprint=window.fingerprint,
        is_complete_snapshot=False,
        params=window.params,
    )
    if run.status != "running":
        logger.info("run_id=%d status=%s already discovered", run.run_id, run.status)
        stored = read_metrics(conn, run.run_id) or RunMetrics()
        return DiscoveryReport(run.run_id, run.status, True, stored)
    logger.info("run_id=%d resumed=%s candidates=%d", run.run_id, run.resumed, len(window.records))

    # Metrics are written with every committed batch, so stored ones mean a resumed run:
    # carry them on and skip the keys its earlier batches already wrote.
    stored = read_metrics(conn, run.run_id) if run.resumed else None
    if stored is None:
        total = _prepare(conn, window, source_id=source_id, run_id=run.run_id)
        remaining = list(window.records)
    else:
        total = stored
        done = _known_keys(conn, source_id, [r.source_key for r in window.records])
        remaining = [r for r in window.records if r.source_key not in done]
    counted = _write_batches(
        conn,
        remaining,
        source_id=source_id,
        run_id=run.run_id,
        batch=batch,
        size=batch_size,
        total=total,
    )

    conn.execute(f"ANALYZE {ANALYZE_TABLES}")
    size = conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]
    metrics = RunMetrics(
        **{
            **asdict(counted),
            "duration_ms": int((clock() - started) * 1000),
            "db_size_bytes": size,
        }
    )
    with conn.transaction():
        write_metrics(conn, run.run_id, metrics)
    finish_run(conn, run.run_id, "ok")
    return DiscoveryReport(run.run_id, "ok", False, metrics)
