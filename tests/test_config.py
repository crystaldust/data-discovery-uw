from datetime import timedelta

import pytest

from ddm.config import (
    ConfigError,
    PodcastIndexConfig,
    load_database_url,
    load_podcastindex_config,
    load_refused_backoff,
    load_user_agent,
    load_web_config,
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


WEB_ENV = {
    "DATABASE_URL_READONLY": "postgresql://reader:pw@host/db",
    "SESSION_SECRET": "s" * 32,
    "DEMO_LOGIN_USER": "demo",
    "DEMO_LOGIN_PASSWORD_HASH": "scrypt$16384$8$1$salt$hash",
}


@pytest.fixture
def web_env(monkeypatch):
    for name, value in WEB_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("DDM_LOCAL_HTTP", raising=False)
    return monkeypatch


def test_web_config_reads_every_setting(web_env):
    config = load_web_config()

    assert config.readonly_db_url == WEB_ENV["DATABASE_URL_READONLY"]
    assert config.demo_user == "demo"
    assert config.secure_cookies is True


@pytest.mark.parametrize("name", sorted(WEB_ENV))
def test_web_config_names_a_missing_variable(web_env, name):
    web_env.delenv(name)

    with pytest.raises(ConfigError, match=name):
        load_web_config()


def test_a_short_session_secret_is_refused_without_showing_it(web_env):
    web_env.setenv("SESSION_SECRET", "short-secret-value")

    with pytest.raises(ConfigError, match="SESSION_SECRET") as error:
        load_web_config()

    assert "short-secret-value" not in str(error.value)


def test_web_config_repr_hides_the_secrets(web_env):
    shown = repr(load_web_config())

    assert "pw@host" not in shown and "s" * 32 not in shown and "scrypt" not in shown
    assert "demo" in shown


def test_local_http_turns_secure_cookies_off(web_env):
    web_env.setenv("DDM_LOCAL_HTTP", "1")

    assert load_web_config().secure_cookies is False


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


def test_podcastindex_config_refuses_a_browser_user_agent(full_env):
    full_env.setenv("DDM_USER_AGENT", "Mozilla/5.0 (+mailto:a@example.com)")

    with pytest.raises(ConfigError, match="honestly"):
        load_podcastindex_config()


def test_podcastindex_config_repr_hides_the_key_and_secret(full_env):
    text = repr(load_podcastindex_config())

    assert "secret-value" not in text
    assert "key-value" not in text
