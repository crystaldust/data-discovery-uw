"""Configuration loaded from environment variables and validated at startup."""

import os
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlsplit, urlunsplit

NEON_HOST_SUFFIX = ".neon.tech"
NEON_POOLER_SUFFIX = "-pooler"
# Outbound requests must say who we are; a browser string would be impersonation.
BROWSER_UA_TOKENS = ("mozilla/", "applewebkit/", "chrome/", "safari/", "gecko/", "firefox/")
UA_CONTACT_MARKERS = ("+mailto:", "+http")
MAX_REFUSED_BACKOFF_DAYS = 365
MIN_SESSION_SECRET_CHARS = 32


class ConfigError(RuntimeError):
    """A required setting is missing. The message names the variable, never its value."""


@dataclass(frozen=True)
class PodcastIndexConfig:
    # Kept out of repr so a traceback or a debug print cannot show them.
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    user_agent: str


@dataclass(frozen=True)
class WebConfig:
    readonly_db_url: str = field(repr=False)
    session_secret: str = field(repr=False)
    demo_user: str
    demo_password_hash: str = field(repr=False)
    secure_cookies: bool = True


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"missing required environment variable {name}")
    return value


def load_podcastindex_config() -> PodcastIndexConfig:
    return PodcastIndexConfig(
        api_key=_require("PODCASTINDEX_API_KEY"),
        api_secret=_require("PODCASTINDEX_API_SECRET"),
        user_agent=load_user_agent(),
    )


def load_user_agent() -> str:
    """The User-Agent for feed fetching: a project name plus a way to reach the operator."""
    value = _require("DDM_USER_AGENT")
    if any(token in value.lower() for token in BROWSER_UA_TOKENS):
        raise ConfigError(
            "DDM_USER_AGENT must identify this project honestly, not imitate a browser"
        )
    if not any(marker in value for marker in UA_CONTACT_MARKERS):
        raise ConfigError("DDM_USER_AGENT must include a contact (+mailto: or +http URL)")
    return value


def load_refused_backoff(default: timedelta) -> timedelta:
    """How long a host that answered 401/403 without Retry-After is left alone; optional."""
    name = "DDM_REFUSED_BACKOFF_DAYS"
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    if not (value.isascii() and value.isdigit()) or not 1 <= int(value) <= MAX_REFUSED_BACKOFF_DAYS:
        raise ConfigError(
            f"{name} must be a whole number of days from 1 to {MAX_REFUSED_BACKOFF_DAYS}"
        )
    return timedelta(days=int(value))


def load_database_url() -> str:
    """Direct (non-pooled) owner connection used by migrations and import jobs."""
    return _require("DATABASE_URL")


def load_web_config() -> WebConfig:
    """Everything the web app needs; all required, so a misconfigured deploy fails at start."""
    secret = _require("SESSION_SECRET")
    if len(secret) < MIN_SESSION_SECRET_CHARS:
        raise ConfigError(f"SESSION_SECRET must be at least {MIN_SESSION_SECRET_CHARS} characters")
    return WebConfig(
        readonly_db_url=_require("DATABASE_URL_READONLY"),
        session_secret=secret,
        demo_user=_require("DEMO_LOGIN_USER"),
        demo_password_hash=_require("DEMO_LOGIN_PASSWORD_HASH"),
        # Only for running on plain http://localhost; every deploy is https.
        secure_cookies=os.environ.get("DDM_LOCAL_HTTP", "").strip() != "1",
    )


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
