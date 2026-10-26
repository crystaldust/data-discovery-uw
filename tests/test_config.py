from datetime import timedelta

import pytest

from ddm.config import (
    ConfigError,
    PodcastIndexConfig,
    load_database_url,
    load_demo_mcp_token,
    load_podcastindex_config,
    load_readonly_database_url,
    load_refused_backoff,
    load_user_agent,
    pooled_neon_url,
)

REQUIRED = {
    "PODCASTINDEX_API_KEY": "key-value",
    "PODCASTINDEX_API_SECRET": "secret-value",
    "DDM_USER_AGENT": "DataDiscoveryDemo/0.1 (+mailto:test@example.com)",
}


@pytest.fixture
def full_env(monkeypatch):
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def test_load_returns_frozen_config_from_environment(full_env):
    config = load_podcastindex_config()

    assert config == PodcastIndexConfig(
        api_key="key-value",
        api_secret="secret-value",
        user_agent="DataDiscoveryDemo/0.1 (+mailto:test@example.com)",
    )
    with pytest.raises(AttributeError):
        config.api_key = "changed"  # type: ignore[misc]


@pytest.mark.parametrize("missing", sorted(REQUIRED))
def test_missing_variable_raises_with_name_but_not_values(full_env, missing):
    full_env.delenv(missing)

    with pytest.raises(ConfigError) as excinfo:
        load_podcastindex_config()

    message = str(excinfo.value)
    assert missing in message
    assert "secret-value" not in message
    assert "key-value" not in message


def test_whitespace_only_value_counts_as_missing(full_env):
    full_env.setenv("PODCASTINDEX_API_SECRET", "   ")

    with pytest.raises(ConfigError, match="PODCASTINDEX_API_SECRET"):
        load_podcastindex_config()


def test_values_are_stripped(full_env):
    full_env.setenv("PODCASTINDEX_API_KEY", "  padded  ")

    assert load_podcastindex_config().api_key == "padded"


def test_demo_mcp_token_is_required(monkeypatch):
    monkeypatch.delenv("DEMO_MCP_TOKEN", raising=False)

    with pytest.raises(ConfigError, match="DEMO_MCP_TOKEN"):
        load_demo_mcp_token()


def test_demo_mcp_token_is_returned_stripped(monkeypatch):
    monkeypatch.setenv("DEMO_MCP_TOKEN", " token-value ")

    assert load_demo_mcp_token() == "token-value"


def test_readonly_database_url_is_optional(monkeypatch):
    monkeypatch.delenv("DATABASE_URL_READONLY", raising=False)

    assert load_readonly_database_url() is None


def test_readonly_database_url_is_returned_when_set(monkeypatch):
    monkeypatch.setenv("DATABASE_URL_READONLY", "postgresql://reader@host/db")

    assert load_readonly_database_url() == "postgresql://reader@host/db"


def test_database_url_is_required_and_error_names_only_the_variable(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ConfigError, match="DATABASE_URL"):
        load_database_url()


def test_database_url_is_returned_stripped(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "  postgresql://owner@host/db  ")

    assert load_database_url() == "postgresql://owner@host/db"


def test_pooled_neon_url_inserts_pooler_after_endpoint_id():
    direct = (
        "postgresql://owner:pw@ep-cool-name-123456.us-east-1.aws.neon.tech/neondb?sslmode=require"
    )

    assert pooled_neon_url(direct) == (
        "postgresql://owner:pw@ep-cool-name-123456-pooler.us-east-1.aws.neon.tech"
        "/neondb?sslmode=require"
    )


def test_pooled_neon_url_leaves_pooled_urls_unchanged():
    pooled = "postgresql://owner:pw@ep-a-1-pooler.eu-central-1.aws.neon.tech/db"

    assert pooled_neon_url(pooled) == pooled


def test_pooled_neon_url_rejects_non_neon_hosts():
    with pytest.raises(ConfigError, match="not a Neon host"):
        pooled_neon_url("postgresql://u:p@localhost:5432/db")


def test_load_user_agent_returns_an_honest_value(monkeypatch):
    monkeypatch.setenv("DDM_USER_AGENT", REQUIRED["DDM_USER_AGENT"])

    assert load_user_agent() == REQUIRED["DDM_USER_AGENT"]


def test_load_user_agent_accepts_a_project_url_as_contact(monkeypatch):
    monkeypatch.setenv("DDM_USER_AGENT", "DataDiscoveryDemo/0.1 (+https://example.com/bot)")

    assert load_user_agent().startswith("DataDiscoveryDemo/")


def test_missing_user_agent_names_the_variable(monkeypatch):
    monkeypatch.delenv("DDM_USER_AGENT", raising=False)

    with pytest.raises(ConfigError, match="DDM_USER_AGENT"):
        load_user_agent()


def test_browser_user_agent_is_refused(monkeypatch):
    monkeypatch.setenv("DDM_USER_AGENT", "Mozilla/5.0 (Macintosh) +mailto:a@example.com")

    with pytest.raises(ConfigError, match="identify this project honestly"):
        load_user_agent()


def test_user_agent_without_contact_is_refused(monkeypatch):
    monkeypatch.setenv("DDM_USER_AGENT", "DataDiscoveryDemo/0.1")

    with pytest.raises(ConfigError, match="contact"):
        load_user_agent()


@pytest.mark.parametrize(
    "value",
    [
        "mozilla/5.0 (+mailto:a@example.com)",
        "Bot/1 AppleWebKit/537.36 Chrome/120 (+mailto:a@example.com)",
    ],
)
def test_browser_tokens_anywhere_are_refused(monkeypatch, value):
    monkeypatch.setenv("DDM_USER_AGENT", value)

    with pytest.raises(ConfigError, match="identify this project honestly"):
        load_user_agent()


DEFAULT_BACKOFF = timedelta(days=30)


@pytest.mark.parametrize("value", [None, "", "  "])
def test_refused_backoff_defaults_when_unset_or_blank(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("DDM_REFUSED_BACKOFF_DAYS", raising=False)
    else:
        monkeypatch.setenv("DDM_REFUSED_BACKOFF_DAYS", value)

    assert load_refused_backoff(DEFAULT_BACKOFF) == DEFAULT_BACKOFF


def test_refused_backoff_is_read_in_days(monkeypatch):
    monkeypatch.setenv("DDM_REFUSED_BACKOFF_DAYS", " 7 ")

    assert load_refused_backoff(DEFAULT_BACKOFF) == timedelta(days=7)


@pytest.mark.parametrize("value", ["0", "-1", "366", "1.5", "a week", "\u00b2"])
def test_refused_backoff_out_of_range_or_not_a_whole_number_names_the_variable(monkeypatch, value):
    monkeypatch.setenv("DDM_REFUSED_BACKOFF_DAYS", value)

    with pytest.raises(ConfigError, match="DDM_REFUSED_BACKOFF_DAYS"):
        load_refused_backoff(DEFAULT_BACKOFF)
