"""The web app's read-only connection pool.

Three layers keep the web side read-only: the database role (SELECT on chosen relations,
EXECUTE on two key functions), fixed queries with bound parameters, and sessions that
default to read-only transactions with a 5 s statement timeout. Those defaults are set
on the role by migration 0006, not here: Neon's PgBouncer pooler pools by transaction
and does not pass startup options through. For the same reason prepared statements are
turned off.
"""

from urllib.parse import urlsplit

from psycopg_pool import ConnectionPool

from ddm.config import NEON_HOST_SUFFIX, pooled_neon_url

POOL_MAX_SIZE = 4
POOL_TIMEOUT_SECONDS = 10
CONNECT_TIMEOUT_SECONDS = 10


def pool_url(url: str) -> str:
    """Neon direct URLs become pooler URLs; anything else (local Postgres) is kept."""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        host = ""
    return pooled_neon_url(url) if host.endswith(NEON_HOST_SUFFIX) else url


def make_pool(url: str) -> ConnectionPool:
    """Not opened here: the app opens it at startup, and min_size=0 means no connection is
    made until the first request (a cold start does not wait for Neon to wake)."""
    return ConnectionPool(
        pool_url(url),
        min_size=0,
        max_size=POOL_MAX_SIZE,
        timeout=POOL_TIMEOUT_SECONDS,
        open=False,
        kwargs={
            "autocommit": True,
            "prepare_threshold": None,
            "connect_timeout": CONNECT_TIMEOUT_SECONDS,
        },
    )
