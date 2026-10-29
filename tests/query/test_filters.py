import pytest

from ddm.query.filters import (
    MAX_OFFSET,
    MAX_PAGE_SIZE,
    MAX_QUERY_CHARS,
    PageRequest,
    QueryInputError,
    SearchFilters,
    page_request,
    parse_filters,
)


def test_no_input_means_no_filters():
    assert parse_filters() == SearchFilters()


def test_blank_strings_count_as_not_set():
    assert parse_filters(q="  ", entity_type="", language=" ", first_seen="") == SearchFilters()


def test_values_are_trimmed_and_language_reduced_to_its_primary_tag():
    filters = parse_filters(q="  b2b  growth ", language="EN-us", category=" Marketing ")

    assert filters.q == "b2b  growth"
    assert filters.language == "en"
    assert filters.category == "marketing"


def test_underscored_language_tags_are_reduced_too():
    assert parse_filters(language="en_US").language == "en"


def test_a_one_character_query_is_refused():
    with pytest.raises(QueryInputError, match="at least 2"):
        parse_filters(q=" a ")


def test_a_query_at_the_limit_is_accepted():
    assert parse_filters(q="x" * MAX_QUERY_CHARS).q == "x" * MAX_QUERY_CHARS


def test_a_query_over_the_limit_is_refused_without_echoing_it():
    secret = "y" * (MAX_QUERY_CHARS + 1)

    with pytest.raises(QueryInputError) as error:
        parse_filters(q=secret)

    assert "q" in str(error.value) and secret not in str(error.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("entity_type", "company"),
        ("first_seen", "observed"),
        ("language", "english!"),
        ("active_within_days", 5),
        ("active_within_days", "30"),
        ("has_contact", "yes"),
        ("outlet", "o" * (MAX_QUERY_CHARS + 1)),
        ("category", "c" * (MAX_QUERY_CHARS + 1)),
    ],
)
def test_invalid_values_are_refused_naming_the_field(field, value):
    with pytest.raises(QueryInputError, match=field):
        parse_filters(**{field: value})


def test_enumerations_and_windows_are_accepted():
    filters = parse_filters(
        entity_type="Journalist", first_seen="discovered", active_within_days=30, has_contact=True
    )

    assert filters == SearchFilters(
        entity_type="journalist", first_seen="discovered", active_within_days=30, has_contact=True
    )


def test_page_request_accepts_the_bounds():
    assert page_request(MAX_PAGE_SIZE, MAX_OFFSET) == PageRequest(MAX_PAGE_SIZE, MAX_OFFSET)
    assert page_request(1, 0, cap=50) == PageRequest(1, 0)


@pytest.mark.parametrize(
    ("limit", "offset", "cap", "field"),
    [
        (0, 0, MAX_PAGE_SIZE, "limit"),
        (MAX_PAGE_SIZE + 1, 0, MAX_PAGE_SIZE, "limit"),
        (51, 0, 50, "limit"),
        (10, -1, MAX_PAGE_SIZE, "offset"),
        (10, MAX_OFFSET + 1, MAX_PAGE_SIZE, "offset"),
        ("10", 0, MAX_PAGE_SIZE, "limit"),
        (True, 0, MAX_PAGE_SIZE, "limit"),
    ],
)
def test_page_request_refuses_out_of_range_values(limit, offset, cap, field):
    with pytest.raises(QueryInputError, match=field):
        page_request(limit, offset, cap=cap)
