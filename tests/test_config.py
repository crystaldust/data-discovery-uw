import pytest

from ddm.config import (
    ConfigError,
    PodcastIndexConfig,
    load_demo_mcp_token,
    load_podcastindex_config,
    load_readonly_database_url,
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
