"""Historical baseline import from the Podcast Index export.

Flow: read the demo slice -> normalise (bad rows become rejects) -> fingerprint the
input -> register the run (an already finished input is skipped) -> in one transaction:
rejects, strong-identifier resolution, upsert with observations, metrics -> ANALYZE ->
mark the run ok. Everything is held in memory, which suits the ~50k-row demo slice;
the client's millions of rows would be streamed in key-range batches instead.

Rejected rows do not make a run 'partial'; they are kept in import_rejects. 'partial' is
reserved for a source that could not be read completely (refresh runs, phase 3).
Known limits handled in later phases: ambiguity is counted within one input (the baseline
always reads the whole slice), and records that already have an entity are not resolved
again when their identifiers change.
"""

import hashlib
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Any

import psycopg

from ddm.ingest.records import (
    NormalizedRecord,
    Reject,
    canonical_json,
    content_hash,
    fingerprint_item,
)
from ddm.ingest.records import input_fingerprint as fingerprint_of
from ddm.ingest.resolve import (
    apply_resolution,
    find_ambiguous_identifiers,
    load_ambiguous_identifiers,
    load_identifier_index,
    plan_resolution,
    write_ambiguous_identifiers,
)
from ddm.ingest.runs import (
    IMPORT_LOCK_KEY,
    RunMetrics,
    finish_run,
    read_metrics,
    register_run,
    source_id_of,
    write_metrics,
    write_rejects,
)
from ddm.ingest.upsert import upsert_records
from ddm.sources.podcastindex_dump import (
    NORMALIZER_VERSION,
    SOURCE_NAME,
    SliceParams,
    iter_slice_rows,
    normalize_dump_row,
)

logger = logging.getLogger(__name__)

BASELINE_CUTOFF = date(2024, 1, 1)
PROVENANCE = "historical"
ENTITY_TYPE = "podcast"
ANALYZE_TABLES = "source_records, observations, entities, entity_identifiers"


@dataclass(frozen=True)
class ImportReport:
    run_id: int
    status: str
    skipped: bool
    metrics: RunMetrics


@dataclass(frozen=True)
class _Input:
    records: tuple[NormalizedRecord, ...]
    rejects: tuple[Reject, ...]
    params: Mapping[str, Any]
    fingerprint: str


def _reject_digest(reject: Reject) -> bytes:
    return hashlib.sha256(canonical_json(dict(reject.raw)).encode()).digest()


def _read_input(dump_path: Path, cutoff: date) -> _Input:
    slice_params = SliceParams(created_before=cutoff)
    records: list[NormalizedRecord] = []
    rejects: list[Reject] = []
    for row_number, row in enumerate(iter_slice_rows(dump_path, slice_params), start=1):
        result = normalize_dump_row(row_number, row)
        (records if isinstance(result, NormalizedRecord) else rejects).append(result)
    # Keys and rejects count too: renumbered rows or a changed bad row make a different input.
    items = [fingerprint_item(r.source_key, content_hash(r)) for r in records] + [
        fingerprint_item(f"reject:{r.row_number}", _reject_digest(r)) for r in rejects
    ]
    params = {**asdict(slice_params), "normalizer_version": NORMALIZER_VERSION}
    return _Input(
        records=tuple(records),
        rejects=tuple(rejects),
        params=MappingProxyType(params),
        fingerprint=fingerprint_of(SOURCE_NAME, params, items),
    )


def _resolved_keys(conn: psycopg.Connection, source_id: int, keys: list[str]) -> set[str]:
    rows = conn.execute(
        """
        SELECT source_key FROM source_records
        WHERE source_id = %s AND entity_id IS NOT NULL AND source_key = ANY(%s)
        """,
        (source_id, keys),
    ).fetchall()
    return {key for (key,) in rows}


def _write_run_data(
    conn: psycopg.Connection,
    data: _Input,
    *,
    source_id: int,
    run_id: int,
    resolver: Callable[..., None],
) -> RunMetrics:
    write_rejects(conn, run_id, data.rejects)
    # Resolve before the upsert so each new row is written once, already carrying its entity.
    resolved = _resolved_keys(conn, source_id, [r.source_key for r in data.records])
    to_resolve = [r for r in data.records if r.source_key not in resolved]
    # Counted over the whole input, so a shared default value is caught before it merges.
    found_ambiguous = find_ambiguous_identifiers(data.records)
    write_ambiguous_identifiers(conn, found_ambiguous, source_id=source_id, run_id=run_id)
    ambiguous = load_ambiguous_identifiers(conn)
    usable = (i for r in to_resolve for i in r.identifiers if i not in ambiguous)
    index = load_identifier_index(conn, usable)
    plan = plan_resolution(to_resolve, index, SOURCE_NAME, ambiguous)
    resolver(
        conn,
        plan,
        source_id=source_id,
        run_id=run_id,
        entity_type=ENTITY_TYPE,
        first_seen_kind=PROVENANCE,
    )
    upserted = upsert_records(
        conn,
        source_id=source_id,
        run_id=run_id,
        provenance=PROVENANCE,
        records=data.records,
        entity_ids=dict(plan.assignments),
    )
    return RunMetrics(
        rows_in=len(data.records) + len(data.rejects),
        rows_rejected=len(data.rejects),
        rows_new=len(upserted.new_keys),
        rows_changed=len(upserted.changed_keys),
        rows_unchanged=upserted.unchanged,
        entities_created=len(plan.new_entities),
        identifiers_added=len(plan.new_identifiers),
        strong_id_merges=plan.strong_id_merges,
        candidates_created=len(plan.candidates),
        ambiguous_identifiers=len(found_ambiguous),
    )


def import_baseline(
    conn: psycopg.Connection,
    dump_path: Path,
    cutoff: date = BASELINE_CUTOFF,
    *,
    clock: Callable[[], float] = time.monotonic,
    resolver: Callable[..., None] = apply_resolution,
) -> ImportReport:
    """Import the slice created before `cutoff`. Expects an autocommit connection."""
    started = clock()
    data = _read_input(dump_path, cutoff)
    source_id = source_id_of(conn, SOURCE_NAME)
    run = register_run(
        conn,
        source_id=source_id,
        run_kind="historical_backfill",
        fingerprint=data.fingerprint,
        is_complete_snapshot=True,
        params=data.params,
    )
    if run.status != "running":
        logger.info("run_id=%d status=%s already imported", run.run_id, run.status)
        stored = read_metrics(conn, run.run_id) or RunMetrics()
        return ImportReport(run.run_id, run.status, True, stored)
    logger.info("run_id=%d resumed=%s rows=%d", run.run_id, run.resumed, len(data.records))

    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (IMPORT_LOCK_KEY,))
        # Metrics are written in the data transaction, so their presence means this run's
        # data is already committed (a crash after commit, or a concurrent twin that won the
        # lock). Replaying would only report zeros over the real counts.
        committed = read_metrics(conn, run.run_id)
        if committed is None:
            counted = _write_run_data(
                conn, data, source_id=source_id, run_id=run.run_id, resolver=resolver
            )
            write_metrics(conn, run.run_id, counted)
        else:
            logger.info("run_id=%d data already committed, finishing only", run.run_id)
            counted = committed

    conn.execute(f"ANALYZE {ANALYZE_TABLES}")
    # Measured after commit: inside the transaction the staging tables would be counted too.
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
    return ImportReport(run.run_id, "ok", False, metrics)
