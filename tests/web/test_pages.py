import csv
import io

import pytest

from tests import seed
from tests.web import world
from tests.web.conftest import DEMO_PASSWORD, DEMO_USER

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/search",
        "/discoveries",
        "/metrics",
        "/export.csv",
        "/entities/00000000-0000-0000-0000-000000000000",
    ],
)
def test_pages_redirect_to_login_when_logged_out(client, path):
    response = client.get(path, follow_redirects=False)

    assert response.status_code == 303 and response.headers["location"] == "/login"


def test_htmx_requests_get_a_401_with_a_redirect_header(client):
    response = client.get("/search", headers={"HX-Request": "true"}, follow_redirects=False)

    assert response.status_code == 401 and response.headers["hx-redirect"] == "/login"


@pytest.mark.parametrize(
    ("username", "password"), [(DEMO_USER, "wrong password!"), ("someone", DEMO_PASSWORD), ("", "")]
)
def test_wrong_credentials_are_refused_with_one_generic_message(client, username, password):
    response = client.post("/login", data={"username": username, "password": password})

    assert response.status_code == 401 and "Wrong username or password." in response.text
    assert client.get("/", follow_redirects=False).status_code == 303


def test_logout_ends_the_session(logged_in):
    logged_in.post("/logout", follow_redirects=False)

    assert logged_in.get("/", follow_redirects=False).status_code == 303


def test_logout_needs_post(logged_in):
    assert logged_in.get("/logout").status_code == 405


def test_search_page_shows_results_with_verification_in_words(logged_in, db):
    world.build(db)

    page = logged_in.get("/", params={"q": "marketing"}).text

    assert "B2B Marketing Show" in page
    assert "✓ checked 2026-10-27" in page
    assert "Old Cooking Hour" not in page


def test_historical_rows_say_so(logged_in, db):
    world.build(db)

    page = logged_in.get("/", params={"q": "cooking"}).text

    assert "historical (imported 2026-10-01)" in page and "from catalogue" in page


def test_htmx_search_returns_only_the_results_fragment(logged_in, db):
    world.build(db)

    fragment = logged_in.get(
        "/search", params={"entity_type": "journalist"}, headers={"HX-Request": "true"}
    ).text
    full = logged_in.get("/search", params={"entity_type": "journalist"}).text

    assert "Some Reporter" in fragment and "<html" not in fragment
    assert "<html" in full


def test_bad_filters_show_an_error_not_a_crash(logged_in, db):
    response = logged_in.get("/", params={"active_within_days": "abc"})
    bad_page = logged_in.get("/", params={"page": "0"})

    assert response.status_code == 400 and "active_within_days" in response.text
    assert bad_page.status_code == 400


def test_paging_links_keep_the_filters(logged_in, db):
    run = seed.run(db, 1)
    for i in range(30):
        eid = seed.entity(db, "podcast", f"Show {i:02d}", run)
        seed.record(db, 1, str(i), eid, run, {"language": "en"})
    seed.refresh(db)

    first = logged_in.get("/", params={"language": "en"}).text
    second = logged_in.get("/", params={"language": "en", "page": "2"}).text

    assert "language=en&amp;page=2" in first
    assert second.count("/entities/") == 5


def test_entity_page_shows_records_masked_routes_and_safe_links(logged_in, db):
    ids = world.build(db)

    page = logged_in.get(f"/entities/{ids['show']}").text

    assert "podcastindex_dump" in page and "podcast_rss" in page
    assert "j***@example.org" in page
    assert seed.SECRET_EMAIL not in page
    assert 'href="javascript:' not in page
    assert 'href="https://feeds.example.com/1.xml"' in page
    assert "noopener noreferrer nofollow" in page


def test_unknown_or_malformed_entity_ids_are_404(logged_in, db):
    assert logged_in.get("/entities/not-a-uuid").status_code == 404
    assert logged_in.get("/entities/00000000-0000-0000-0000-000000000000").status_code == 404


def test_discoveries_page_filters_and_rejects_unknown_values(logged_in, db):
    world.build(db)

    page = logged_in.get("/discoveries", params={"classification": "new"})
    bad = logged_in.get("/discoveries", params={"source": "elsewhere"})

    assert page.status_code == 200 and "Some Reporter" in page.text
    assert bad.status_code == 400


def test_metrics_page_links_the_accuracy_report(logged_in, db):
    world.build(db)

    page = logged_in.get("/metrics").text

    assert "eval/report.md" in page and "B2B" not in page
    assert seed.SECRET_EMAIL not in page


def test_export_downloads_every_matching_row_without_emails(logged_in, db):
    world.build(db)

    response = logged_in.get("/export.csv", params={"entity_type": "podcast"})
    rows = list(csv.reader(io.StringIO(response.text)))

    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"].startswith('attachment; filename="entities-')
    assert rows[0][:3] == ["entity_id", "entity_type", "display_name"]
    assert {r[2] for r in rows[1:]} == {"B2B Marketing Show", "Old Cooking Hour"}
    assert seed.SECRET_EMAIL not in response.text


def test_export_with_bad_filters_is_400(logged_in, db):
    assert logged_in.get("/export.csv", params={"entity_type": "company"}).status_code == 400


def test_page_titles_from_the_data_are_escaped(logged_in, db):
    run = seed.run(db, 1)
    eid = seed.entity(db, "podcast", "<script>alert(1)</script>", run)
    seed.record(db, 1, "x", eid, run, {})
    seed.refresh(db)

    search = logged_in.get("/").text
    detail = logged_in.get(f"/entities/{eid}").text

    assert "<script>alert(1)</script>" not in search + detail
    assert "&lt;script&gt;" in search


@pytest.mark.parametrize("value", ["9" * 4400, "9999999"])
def test_huge_numbers_in_the_url_are_a_400_not_a_crash(logged_in, db, value):
    assert logged_in.get("/", params={"page": value}).status_code == 400
    assert logged_in.get("/", params={"active_within_days": value}).status_code == 400


def test_htmx_errors_come_back_as_200_fragments_so_they_are_shown(logged_in, db):
    response = logged_in.get(
        "/search", params={"active_within_days": "45"}, headers={"HX-Request": "true"}
    )

    assert response.status_code == 200 and "active_within_days" in response.text


def test_an_unchecked_contact_box_value_does_not_filter(logged_in, db):
    world.build(db)

    page = logged_in.get("/", params={"has_contact": "false"}).text

    assert "Old Cooking Hour" in page


def test_too_many_parallel_exports_get_429(logged_in, db):
    from ddm.web.pages import EXPORT_SLOTS, MAX_CONCURRENT_EXPORTS

    for _ in range(MAX_CONCURRENT_EXPORTS):
        assert EXPORT_SLOTS.acquire(blocking=False)
    try:
        response = logged_in.get("/export.csv")
    finally:
        for _ in range(MAX_CONCURRENT_EXPORTS):
            EXPORT_SLOTS.release()

    assert response.status_code == 429 and response.headers["retry-after"] == "60"


def test_an_export_gives_its_slot_and_connection_back(logged_in, db, pool):
    from ddm.web.pages import MAX_CONCURRENT_EXPORTS

    for _ in range(MAX_CONCURRENT_EXPORTS + 2):
        assert logged_in.get("/export.csv").status_code == 200

    assert pool.get_stats().get("requests_waiting", 0) == 0


def test_repeated_failed_logins_lock_the_client_out_even_with_the_right_password(client):
    from ddm.web.auth import MAX_FAILURES_PER_CLIENT

    for _ in range(MAX_FAILURES_PER_CLIENT):
        client.post("/login", data={"username": DEMO_USER, "password": "wrong password!"})
    blocked = client.post(
        "/login", data={"username": DEMO_USER, "password": DEMO_PASSWORD}, follow_redirects=False
    )

    assert blocked.status_code == 429 and "Too many failed attempts" in blocked.text


def test_an_abandoned_export_closes_its_connection_instead_of_reusing_it(db, pool):
    from ddm.web.pages import EXPORT_SLOTS, MAX_CONCURRENT_EXPORTS, ExportLease

    assert EXPORT_SLOTS.acquire(blocking=False)  # the route takes the slot first
    lease = ExportLease(pool)
    conn = lease.conn
    conn.execute("BEGIN")  # as a suspended export would leave it

    lease.abandon()
    lease.finish()  # a later normal finish is a no-op

    assert conn.closed
    for _ in range(MAX_CONCURRENT_EXPORTS):
        assert EXPORT_SLOTS.acquire(blocking=False)
    for _ in range(MAX_CONCURRENT_EXPORTS):
        EXPORT_SLOTS.release()
