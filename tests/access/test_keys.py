import threading

import psycopg
import pytest

from ddm.access.keys import (
    KEY_PREFIX,
    MAX_KEY_CHARS,
    check_key,
    create_key,
    hash_key,
    meter,
    new_key,
    revoke_key,
)
from ddm.db.migrate import migrate


def test_new_keys_are_prefixed_and_unique():
    keys = {new_key() for _ in range(20)}

    assert len(keys) == 20
    assert all(k.startswith(KEY_PREFIX) and len(k) > 40 for k in keys)


def test_hash_key_is_a_stable_32_byte_digest():
    assert hash_key("ddm_x") == hash_key("ddm_x")
    assert len(hash_key("ddm_x")) == 32 and hash_key("ddm_x") != hash_key("ddm_y")


@pytest.fixture
def db(reader_db):
    migrate(reader_db)
    return reader_db


@pytest.fixture
def reader(db, reader_url):
    with psycopg.connect(reader_url, autocommit=True) as conn:
        conn.execute("SET default_transaction_read_only = on")
        yield conn


@pytest.mark.integration
def test_a_created_key_checks_until_it_is_revoked(db, reader):
    key_id, key = create_key(db, "demo")

    before = check_key(reader, key)
    revoked = revoke_key(db, key_id)
    after = check_key(reader, key)

    assert before == key_id and revoked and after is None
    assert revoke_key(db, key_id) is False


@pytest.mark.integration
def test_only_the_hash_is_stored(db):
    _, key = create_key(db, "demo")

    stored = db.execute("SELECT key_hash, label FROM api_keys").fetchone()

    assert stored == (hash_key(key), "demo")


@pytest.mark.integration
@pytest.mark.parametrize("key", [None, "", "x" * (MAX_KEY_CHARS + 1), "ddm_wrong"])
def test_implausible_or_unknown_keys_are_refused(db, reader, key):
    assert check_key(reader, key) is None


@pytest.mark.integration
def test_meter_refuses_the_call_after_the_limit_from_a_read_only_session(db, reader, fresh_minute):
    key_id, _ = create_key(db, "demo", rate_limit=60)

    results = [meter(reader, key_id, "api", "entities") for _ in range(61)]

    assert all(r.allowed for r in results[:60])
    assert not results[60].allowed and 1 <= results[60].retry_after_seconds <= 60


@pytest.mark.integration
def test_api_and_mcp_calls_share_one_limit(db, reader, fresh_minute):
    key_id, _ = create_key(db, "demo", rate_limit=2)

    meter(reader, key_id, "api", "entities")
    meter(reader, key_id, "mcp", "search_entities")

    assert not meter(reader, key_id, "mcp", "get_entity").allowed


@pytest.mark.integration
def test_concurrent_calls_are_counted_exactly(db, reader_url, fresh_minute):
    key_id, _ = create_key(db, "demo", rate_limit=60)
    allowed = []
    lock = threading.Lock()

    def calls():
        with psycopg.connect(reader_url, autocommit=True) as conn:
            mine = [meter(conn, key_id, "api", "entities").allowed for _ in range(40)]
        with lock:
            allowed.extend(mine)

    threads = [threading.Thread(target=calls) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    total = db.execute("SELECT sum(calls) FROM api_usage").fetchone()[0]

    assert total == 80
    assert allowed.count(True) == 60
