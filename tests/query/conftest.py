import psycopg
import pytest

from ddm.db.migrate import migrate


@pytest.fixture
def db(reader_db):
    """Owner connection on a migrated database: seed through this one."""
    migrate(reader_db)
    return reader_db


@pytest.fixture
def reader(db, reader_url):
    """Read-only connection: query through this one, as the web app does."""
    with psycopg.connect(reader_url, autocommit=True) as conn:
        yield conn
