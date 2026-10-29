import pytest

from ddm.query.scrub import scrub, scrub_text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Pitch us at jane.doe@show.example.com today", "Pitch us at j***@show.example.com today"),
        ("abc@example.org", "***@example.org"),
        ("mailto:booking@example.org", "mailto:b***@example.org"),
        ("https://user:pw@feeds.example.com/a.xml", "https://feeds.example.com/a.xml"),
        (
            "https://x.example.com/f?email=jane.doe@example.org",
            "https://x.example.com/f?email=j***@example.org",
        ),
        ("@handle on X", "@handle on X"),
        ("already j***@example.org", "already j***@example.org"),
        ("no address here", "no address here"),
    ],
)
def test_scrub_text_masks_addresses_and_url_credentials(text, expected):
    assert scrub_text(text) == expected


def test_scrub_walks_nested_values_and_keeps_other_types():
    value = {"bylines": [{"title": "By jane.doe@example.org", "n": 3}], "tags": ("a", "b@c.de")}

    assert scrub(value) == {
        "bylines": [{"title": "By j***@example.org", "n": 3}],
        "tags": ("a", "***@c.de"),
    }
    assert scrub(None) is None and scrub(5) == 5
