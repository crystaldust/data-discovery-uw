"""Configuration loaded from environment variables and validated at startup."""

import os
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

NEON_HOST_SUFFIX = ".neon.tech"
NEON_POOLER_SUFFIX = "-pooler"


class ConfigError(RuntimeError):
    """A required setting is missing. The message names the variable, never its value."""


@dataclass(frozen=True)
class PodcastIndexConfig:
    api_key: str
    api_secret: str
    user_agent: str


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"missing required environment variable {name}")
    return value


def load_podcastindex_config() -> PodcastIndexConfig:
    return PodcastIndexConfig(
        api_key=_require("PODCASTINDEX_API_KEY"),
        api_secret=_require("PODCASTINDEX_API_SECRET"),
        user_agent=_require("DDM_USER_AGENT"),
    )


def load_demo_mcp_token() -> str:
    return _require("DEMO_MCP_TOKEN")


def load_database_url() -> str:
    """Direct (non-pooled) owner connection used by migrations and import jobs."""
    return _require("DATABASE_URL")


def load_readonly_database_url() -> str | None:
    return os.environ.get("DATABASE_URL_READONLY", "").strip() or None


def pooled_neon_url(direct_url: str) -> str:
    """Return the PgBouncer (pooler) URL for a Neon direct URL; pooled URLs pass through."""
    parts = urlsplit(direct_url)
    host = parts.hostname or ""
    if not host.endswith(NEON_HOST_SUFFIX):
        raise ConfigError("database URL is not a Neon host")
    endpoint, _, rest = host.partition(".")
    if endpoint.endswith(NEON_POOLER_SUFFIX):
        return direct_url
    netloc = parts.netloc.replace(host, f"{endpoint}{NEON_POOLER_SUFFIX}.{rest}", 1)
    return urlunsplit(parts._replace(netloc=netloc))
