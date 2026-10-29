"""Cost report: what the jobs and the service used, how much of each free tier that is,
and what it would cost on the cheapest paid tier.

Only measured numbers and published prices. Neon compute hours are not visible from
inside the database, so they are not estimated here (see the Neon console).
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime

import psycopg
from psycopg.rows import dict_row

COST_WINDOW_DAYS = 30
BYTES_PER_MB = 1024 * 1024
BYTES_PER_GB = 1024**3

# Published prices, checked on PRICES_AS_OF at the PRICE_SOURCES pages.
PRICES_AS_OF = "2026-10-29"
PRICE_SOURCES = (
    "https://neon.com/pricing",
    "https://docs.github.com/en/billing/concepts/product-billing/github-actions",
    "https://vercel.com/docs/plans/hobby",
)
NEON_FREE_STORAGE_BYTES = 1 * BYTES_PER_GB  # Free plan, per project
NEON_STORAGE_USD_PER_GB_MONTH = 0.35  # Launch plan
ACTIONS_PRIVATE_USD_PER_MINUTE = 0.006  # Linux 2-core; public repositories are free
VERCEL_HOBBY_INVOCATIONS = 1_000_000  # Hobby, per month

SOURCES = f"""
SELECT s.name AS source,
       count(*)::int AS runs,
       count(*) FILTER (WHERE r.status = 'ok')::int AS ok,
       count(*) FILTER (WHERE r.status = 'partial')::int AS partial,
       count(*) FILTER (WHERE r.status = 'failed')::int AS failed,
       coalesce(sum(m.http_requests), 0)::bigint AS http_requests,
       coalesce(sum(m.bytes_fetched), 0)::bigint AS bytes_fetched,
       coalesce(sum(m.duration_ms), 0)::bigint AS duration_ms
FROM import_runs r
JOIN sources s USING (source_id)
LEFT JOIN run_metrics m USING (run_id)
WHERE r.started_at >= %(now)s - interval '{COST_WINDOW_DAYS} days'
GROUP BY s.source_id, s.name
ORDER BY s.source_id"""  # noqa: S608 - only module constants are interpolated

API_CALLS = f"""
SELECT (day AT TIME ZONE 'UTC')::date AS day, surface, calls
FROM api_usage_daily
WHERE day >= %(now)s - interval '{COST_WINDOW_DAYS} days'
ORDER BY day, surface"""  # noqa: S608 - only module constants are interpolated

DB_SIZE = "SELECT pg_database_size(current_database())"


@dataclass(frozen=True)
class SourceCost:
    source: str
    runs: int
    ok: int
    partial: int
    failed: int
    http_requests: int
    mb_fetched: float
    minutes: float  # the jobs' own run time, without runner setup


@dataclass(frozen=True)
class ResourceUse:
    resource: str
    used: float
    unit: str
    free_limit: float | None  # None: no limit (free on public repositories)
    share_of_free: float | None
    paid_estimate_usd: float | None  # None: not estimated
    note: str


@dataclass(frozen=True)
class ApiCalls:
    day: date
    surface: str
    calls: int


@dataclass(frozen=True)
class Costs:
    window_days: int
    within_free_tiers: bool  # every limited resource is below its free limit
    resources: tuple[ResourceUse, ...]
    sources: tuple[SourceCost, ...]
    api_calls: tuple[ApiCalls, ...]
    prices_as_of: str
    price_sources: tuple[str, ...]


def _source(row: dict) -> SourceCost:
    return SourceCost(
        source=row["source"],
        runs=row["runs"],
        ok=row["ok"],
        partial=row["partial"],
        failed=row["failed"],
        http_requests=row["http_requests"],
        mb_fetched=round(row["bytes_fetched"] / BYTES_PER_MB, 1),
        minutes=round(row["duration_ms"] / 60_000, 1),
    )


def _resources(
    db_bytes: int, sources: tuple[SourceCost, ...], api_calls: tuple[ApiCalls, ...]
) -> tuple[ResourceUse, ...]:
    storage_gb = db_bytes / BYTES_PER_GB
    minutes = sum(s.minutes for s in sources)
    calls = sum(c.calls for c in api_calls)
    return (
        ResourceUse(
            resource="Neon storage",
            used=round(db_bytes / BYTES_PER_MB, 1),
            unit="MB",
            free_limit=NEON_FREE_STORAGE_BYTES / BYTES_PER_MB,
            share_of_free=round(db_bytes / NEON_FREE_STORAGE_BYTES, 3),
            paid_estimate_usd=round(storage_gb * NEON_STORAGE_USD_PER_GB_MONTH, 2),
            note="logical database size, per month on Neon Launch; history and compute "
            "hours are billed separately (see the Neon console)",
        ),
        ResourceUse(
            resource="GitHub Actions minutes",
            used=round(minutes, 1),
            unit="min",
            free_limit=None,
            share_of_free=None,
            paid_estimate_usd=round(minutes * ACTIONS_PRIVATE_USD_PER_MINUTE, 2),
            note="free on a public repository; estimate is for a private one; counts the "
            "jobs' own run time only (no runner setup, CI or backups), so it is a lower bound",
        ),
        ResourceUse(
            resource="Vercel function calls",
            used=calls,
            unit="calls",
            free_limit=VERCEL_HOBBY_INVOCATIONS,
            share_of_free=round(calls / VERCEL_HOBBY_INVOCATIONS, 4),
            paid_estimate_usd=None,
            note="API and MCP calls only (a lower bound); Hobby is non-commercial only, "
            "a client deployment needs Pro",
        ),
    )


def get_costs(conn: psycopg.Connection, now: datetime | None = None) -> Costs:
    params = {"now": now or datetime.now(UTC)}
    with conn.cursor(row_factory=dict_row) as cur:
        sources = tuple(_source(r) for r in cur.execute(SOURCES, params).fetchall())
        api_calls = tuple(ApiCalls(**r) for r in cur.execute(API_CALLS, params).fetchall())
        db_bytes = cur.execute(DB_SIZE).fetchone()["pg_database_size"]
    resources = _resources(db_bytes, sources, api_calls)
    return Costs(
        window_days=COST_WINDOW_DAYS,
        within_free_tiers=all(r.used < r.free_limit for r in resources if r.free_limit is not None),
        resources=resources,
        sources=sources,
        api_calls=api_calls,
        prices_as_of=PRICES_AS_OF,
        price_sources=PRICE_SOURCES,
    )
