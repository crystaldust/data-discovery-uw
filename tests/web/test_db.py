import psycopg
import pytest

from ddm.access.keys import create_key, meter
from ddm.web.db import pool_url


def test_a_neon_direct_url_becomes_the_pooler_url():
    url = "postgresql://reader:pw@ep-cool-1.ap-southeast-2.aws.neon.tech/db?sslmode=require"

    assert pool_url(url) == (
        "postgresql://reader:pw@ep-cool-1-pooler.ap-southeast-2.aws.neon.tech/db?sslmode=require"
    )


def test_a_local_url_is_kept():
    url = "postgresql://reader:pw@127.0.0.1:54329/ddm"

    assert pool_url(url) == url


@pytest.mark.integration
def test_pool_sessions_are_read_only_and_time_limited(pool):
    with pool.connection() as conn:
        read_only = conn.execute("SHOW default_transaction_read_only").fetchone()[0]
        timeout = conn.execute("SHOW statement_timeout").fetchone()[0]

    assert read_only == "on" and timeout == "5s"


@pytest.mark.integration
def test_pool_sessions_cannot_write_even_temporary_tables(pool):
    with pool.connection() as conn, pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        conn.execute("CREATE TEMP TABLE t (id int)")


@pytest.mark.integration
def test_metering_works_through_the_pool(db, pool):
    key_id, _ = create_key(db, "demo")

    with pool.connection() as conn:
        result = meter(conn, key_id, "api", "entities")

    assert result.allowed
