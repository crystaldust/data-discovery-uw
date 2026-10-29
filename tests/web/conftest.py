import pytest

from ddm.db.migrate import migrate
from ddm.web.db import make_pool


@pytest.fixture
def db(reader_db):
    """Owner connection on a migrated database: seed through this one."""
    migrate(reader_db)
    return reader_db


@pytest.fixture
def pool(db, reader_url):
    """The web app's read-only pool against the test database."""
    pool = make_pool(reader_url)
    pool.open()
    yield pool
    pool.close()


DEMO_USER = "demo"
DEMO_PASSWORD = "a long test password"
SESSION_SECRET = "x" * 32


@pytest.fixture(scope="session")
def demo_password_hash() -> str:
    from ddm.access.passwords import hash_password

    return hash_password(DEMO_PASSWORD)


@pytest.fixture
def web_config(reader_url, demo_password_hash):
    from ddm.config import WebConfig

    return WebConfig(
        readonly_db_url=reader_url,
        session_secret=SESSION_SECRET,
        demo_user=DEMO_USER,
        demo_password_hash=demo_password_hash,
        secure_cookies=False,
    )


@pytest.fixture
def client(web_config, pool):
    from fastapi.testclient import TestClient

    from ddm.web.app import create_app

    with TestClient(create_app(web_config, pool=pool)) as client:
        yield client


@pytest.fixture
def logged_in(client):
    response = client.post(
        "/login", data={"username": DEMO_USER, "password": DEMO_PASSWORD}, follow_redirects=False
    )
    assert response.status_code == 303
    return client


@pytest.fixture
def api_key(db):
    from ddm.access.keys import create_key

    return create_key(db, "test", rate_limit=60)
