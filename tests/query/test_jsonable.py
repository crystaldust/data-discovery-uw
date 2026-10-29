import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from ddm.query.jsonable import to_jsonable


@dataclass(frozen=True)
class Inner:
    when: datetime
    tags: tuple[str, ...]


@dataclass(frozen=True)
class Outer:
    id: uuid.UUID
    inner: Inner
    attrs: dict
    day: date
    score: float | None


def test_nested_results_become_plain_json_values():
    eid = uuid.uuid4()
    value = Outer(
        id=eid,
        inner=Inner(datetime(2026, 10, 29, tzinfo=UTC), ("a", "b")),
        attrs={"k": [1, {"n": None}]},
        day=date(2026, 10, 1),
        score=None,
    )

    assert to_jsonable(value) == {
        "id": str(eid),
        "inner": {"when": "2026-10-29T00:00:00+00:00", "tags": ["a", "b"]},
        "attrs": {"k": [1, {"n": None}]},
        "day": "2026-10-01",
        "score": None,
    }
