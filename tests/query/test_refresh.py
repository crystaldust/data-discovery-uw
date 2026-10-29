import pytest

from ddm.query.refresh import refresh_search
from tests import seed

pytestmark = pytest.mark.integration


def test_refresh_makes_new_entities_searchable(db, reader):
    run = seed.run(db, 1)
    seed.entity(db, "podcast", "Fresh Show", run)

    before = reader.execute("SELECT count(*) FROM entity_search").fetchone()[0]
    rows = refresh_search(db)
    after = reader.execute("SELECT display_name FROM entity_search").fetchall()

    assert before == 0 and rows == 1 and after == [("Fresh Show",)]
