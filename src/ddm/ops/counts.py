"""Row counts of every table and materialized view, to check a restored backup against
the database it was taken from. Counts are numbers only, safe to publish."""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql

RELATIONS = """
SELECT tablename FROM pg_tables WHERE schemaname = 'public'
UNION
SELECT matviewname FROM pg_matviews WHERE schemaname = 'public'
ORDER BY 1"""


class CountsInputError(ValueError):
    """A counts file is missing, unreadable or not in the expected shape."""


@dataclass(frozen=True)
class CountDiff:
    relation: str
    expected: int | None  # None: not in the expected counts
    actual: int | None  # None: missing from this database


def table_counts(conn: psycopg.Connection, exclude: Iterable[str] = ()) -> dict[str, int]:
    """exclude: relations that change while the service runs (api_usage takes a row per
    API call), so they cannot be compared across a backup."""
    skipped = set(exclude)
    names = [r[0] for r in conn.execute(RELATIONS).fetchall() if r[0] not in skipped]
    query = sql.SQL("SELECT count(*) FROM {}")
    return {n: conn.execute(query.format(sql.Identifier(n))).fetchone()[0] for n in names}


def compare_counts(expected: Mapping[str, int], actual: Mapping[str, int]) -> tuple[CountDiff, ...]:
    return tuple(
        CountDiff(name, expected.get(name), actual.get(name))
        for name in sorted(expected.keys() | actual.keys())
        if expected.get(name) != actual.get(name)
    )


def read_counts(path: Path) -> dict[str, int]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CountsInputError(f"cannot read counts from {path}") from exc
    valid = isinstance(data, dict) and all(
        isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
        for k, v in data.items()
    )
    if not valid:
        raise CountsInputError(f"{path} is not a JSON object of table name to row count")
    return data


def counts_json(counts: Mapping[str, int]) -> str:
    return json.dumps(dict(sorted(counts.items())), indent=2) + "\n"
