import pytest

from ddm.access.keys import create_key, revoke_key
from tests import seed
from tests.web import world

pytestmark = pytest.mark.integration


def auth(key):
    return {"Authorization": f"Bearer {key}"}


def test_entities_are_listed_with_page_meta(client, db, api_key):
    world.build(db)

    response = client.get(
        "/api/v1/entities", params={"category": "marketing"}, headers=auth(api_key[1])
    )

    body = response.json()
    assert response.status_code == 200
    assert [e["display_name"] for e in body["data"]] == ["B2B Marketing Show"]
    assert body["meta"] == {"limit": 20, "offset": 0, "has_more": False}
    assert body["data"][0]["activity_basis"] == "observed"


def test_every_filter_is_accepted_from_a_query_string(client, db, api_key):
    world.build(db)
    params = {
        "q": "marketing",
        "entity_type": "podcast",
        "language": "en",
        "category": "marketing",
        "active_within_days": "30",
        "has_contact": "true",
        "first_seen": "historical",
        "limit": "5",
        "offset": "0",
    }

    response = client.get("/api/v1/entities", params=params, headers=auth(api_key[1]))

    assert response.status_code == 200
    assert [e["display_name"] for e in response.json()["data"]] == ["B2B Marketing Show"]


def test_an_x_api_key_header_is_accepted_too(client, db, api_key):
    response = client.get("/api/v1/metrics", headers={"X-API-Key": api_key[1]})

    assert response.status_code == 200


def test_entity_detail_and_its_errors(client, db, api_key):
    ids = world.build(db)
    headers = auth(api_key[1])

    found = client.get(f"/api/v1/entities/{ids['show']}", headers=headers)
    bad = client.get("/api/v1/entities/not-a-uuid", headers=headers)
    missing = client.get("/api/v1/entities/00000000-0000-0000-0000-000000000000", headers=headers)

    assert found.status_code == 200
    assert found.json()["data"]["contact_routes"][0]["display_value"] == "j***@example.org"
    assert bad.status_code == 404 and missing.status_code == 404
    assert missing.json() == {"error": {"code": "not_found", "message": "no entity with that id"}}


def test_discoveries_and_metrics(client, db, api_key):
    world.build(db)
    headers = auth(api_key[1])

    discoveries = client.get(
        "/api/v1/discoveries", params={"classification": "new"}, headers=headers
    ).json()
    metrics = client.get("/api/v1/metrics", headers=headers).json()

    assert [d["entity_name"] for d in discoveries["data"]] == ["Some Reporter"]
    assert metrics["data"]["pending_candidates"] == 0


def test_no_response_contains_an_email(client, db, api_key):
    ids = world.build(db)
    headers = auth(api_key[1])

    bodies = [
        client.get(path, headers=headers).text
        for path in (
            "/api/v1/entities",
            f"/api/v1/entities/{ids['show']}",
            "/api/v1/discoveries",
            "/api/v1/metrics",
        )
    ]

    assert all(seed.SECRET_EMAIL not in body for body in bodies)


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer ddm_wrong"}, {"Authorization": "Basic Zm9vOmJhcg=="}]
)
def test_requests_without_a_valid_key_are_refused(client, db, headers):
    response = client.get("/api/v1/entities", headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "unauthorized"


def test_a_revoked_key_is_refused(client, db, api_key):
    revoke_key(db, api_key[0])

    assert client.get("/api/v1/metrics", headers=auth(api_key[1])).status_code == 401


def test_calls_over_the_limit_get_429_with_retry_after(client, db, fresh_minute):
    _, key = create_key(db, "tight", rate_limit=2)

    codes = [client.get("/api/v1/metrics", headers=auth(key)).status_code for _ in range(3)]
    refused = client.get("/api/v1/metrics", headers=auth(key))

    assert codes == [200, 200, 429]
    assert refused.status_code == 429
    assert 1 <= int(refused.headers["retry-after"]) <= 60
    assert refused.json()["error"]["code"] == "rate_limited"


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"limit": 101}, "limit"),
        ({"offset": 10001}, "offset"),
        ({"entity_type": "x"}, "entity_type"),
        ({"active_within_days": 5}, "active_within_days"),
        ({"q": "q" * 201}, "q"),
    ],
)
def test_invalid_parameters_get_422_naming_the_field_only(client, db, api_key, params, field):
    response = client.get("/api/v1/entities", params=params, headers=auth(api_key[1]))

    assert response.status_code == 422
    message = response.json()["error"]["message"]
    assert field in message and "q" * 201 not in message


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_only_reads_are_allowed(client, db, api_key, method):
    response = getattr(client, method)("/api/v1/entities", headers=auth(api_key[1]))

    assert response.status_code == 405


def test_the_openapi_document_lists_the_four_endpoints_and_bearer_auth(client):
    spec = client.get("/api/v1/openapi.json").json()

    assert set(spec["paths"]) == {
        "/api/v1/entities",
        "/api/v1/entities/{entity_id}",
        "/api/v1/discoveries",
        "/api/v1/metrics",
    }
    assert any(s.get("scheme") == "bearer" for s in spec["components"]["securitySchemes"].values())


def test_docs_page_is_public(client):
    assert client.get("/docs").status_code == 200


def test_discovery_sources_are_the_ones_that_classify(client, db, api_key):
    headers = auth(api_key[1])

    ok = client.get("/api/v1/discoveries", params={"source": "outlet_rss"}, headers=headers)
    bad = client.get("/api/v1/discoveries", params={"source": "wikidata"}, headers=headers)

    assert ok.status_code == 200 and bad.status_code == 422


def test_metrics_include_costs(client, db, api_key):
    body = client.get("/api/v1/metrics", headers=auth(api_key[1])).json()

    costs = body["data"]["costs"]
    assert costs["within_free_tiers"] is True
    assert [r["resource"] for r in costs["resources"]] == [
        "Neon storage",
        "GitHub Actions minutes",
        "Vercel function calls",
    ]
