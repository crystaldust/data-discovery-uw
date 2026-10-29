"""Search filters and paging, validated once for every entry point.

Error messages name the field, never the value: a value can be anything a caller typed.
"""

import re
from dataclasses import dataclass
from typing import Any

MAX_QUERY_CHARS = 200
# One character matches nearly every name and no index can narrow it.
MIN_QUERY_CHARS = 2
MAX_PAGE_SIZE = 100
MCP_MAX_RESULTS = 50
MAX_OFFSET = 10_000
ACTIVE_WINDOWS_DAYS = (7, 30, 90, 365)
ENTITY_TYPES = ("podcast", "journalist")
FIRST_SEEN_KINDS = ("historical", "discovered")
LANGUAGE_PATTERN = re.compile(r"[a-z]{2,3}")


class QueryInputError(ValueError):
    """A filter or paging value is not acceptable. The message names the field only."""


@dataclass(frozen=True)
class SearchFilters:
    q: str | None = None
    entity_type: str | None = None
    language: str | None = None
    category: str | None = None
    outlet: str | None = None
    active_within_days: int | None = None
    has_contact: bool | None = None
    first_seen: str | None = None


@dataclass(frozen=True)
class PageRequest:
    limit: int
    offset: int


def _text(field: str, value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise QueryInputError(f"{field} must be text")
    text = value.strip()
    if len(text) > MAX_QUERY_CHARS:
        raise QueryInputError(f"{field} is longer than {MAX_QUERY_CHARS} characters")
    return text or None


def choice(field: str, value: Any, choices: tuple[str, ...]) -> str | None:
    """One of choices (case-insensitive), or None when blank."""
    text = _text(field, value)
    if text is None:
        return None
    if text.lower() not in choices:
        raise QueryInputError(f"{field} must be one of {', '.join(choices)}")
    return text.lower()


def _language(value: Any) -> str | None:
    text = _text("language", value)
    if text is None:
        return None
    primary = text.lower().replace("_", "-").split("-", 1)[0]
    if not LANGUAGE_PATTERN.fullmatch(primary):
        raise QueryInputError("language must be a language code such as en")
    return primary


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _window(value: Any) -> int | None:
    if value is None:
        return None
    if not _is_int(value) or value not in ACTIVE_WINDOWS_DAYS:
        windows = ", ".join(str(d) for d in ACTIVE_WINDOWS_DAYS)
        raise QueryInputError(f"active_within_days must be one of {windows}")
    return value


def _flag(value: Any) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    raise QueryInputError("has_contact must be true or false")


def parse_filters(
    *,
    q: Any = None,
    entity_type: Any = None,
    language: Any = None,
    category: Any = None,
    outlet: Any = None,
    active_within_days: Any = None,
    has_contact: Any = None,
    first_seen: Any = None,
) -> SearchFilters:
    category_text = _text("category", category)
    query = _text("q", q)
    if query is not None and len(query) < MIN_QUERY_CHARS:
        raise QueryInputError(f"q needs at least {MIN_QUERY_CHARS} characters")
    return SearchFilters(
        q=query,
        entity_type=choice("entity_type", entity_type, ENTITY_TYPES),
        language=_language(language),
        category=category_text.lower() if category_text else None,
        outlet=_text("outlet", outlet),
        active_within_days=_window(active_within_days),
        has_contact=_flag(has_contact),
        first_seen=choice("first_seen", first_seen, FIRST_SEEN_KINDS),
    )


def page_request(limit: Any, offset: Any = 0, cap: int = MAX_PAGE_SIZE) -> PageRequest:
    if not _is_int(limit) or not 1 <= limit <= cap:
        raise QueryInputError(f"limit must be between 1 and {cap}")
    if not _is_int(offset) or not 0 <= offset <= MAX_OFFSET:
        raise QueryInputError(f"offset must be between 0 and {MAX_OFFSET}")
    return PageRequest(limit, offset)
