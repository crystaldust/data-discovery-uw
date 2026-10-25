"""Configuration loaded from environment variables and validated at startup."""

import os
from dataclasses import dataclass


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


def load_readonly_database_url() -> str | None:
    return os.environ.get("DATABASE_URL_READONLY", "").strip() or None
