"""Strong identifiers and neutral entity ids.

Only exact matches on these normalised identifiers may merge records automatically.
Normalisation is conservative: it removes differences that cannot change which feed
is meant (scheme, host case, default port, fragment, trailing slash) and nothing else.
"""

import uuid
from typing import Any
from urllib.parse import urlsplit

# Never change: every entity id is derived from it, so changing it re-keys the database.
DDM_ENTITY_NAMESPACE = uuid.UUID("5e3d58e3-60b5-4f04-9754-80ca913058a9")

DEFAULT_PORTS = {"http": 80, "https": 443}


def is_ascii_digits(value: str) -> bool:
    """True for '0'-'9' only; str.isdigit() also accepts characters such as '²'."""
    return value.isascii() and value.isdecimal()


def normalize_feed_url(url: str | None) -> str | None:
    """Scheme-less canonical form; http and https of the same address count as one feed."""
    if url is None:
        return None
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS:
        return None
    host = (parts.hostname or "").rstrip(".")
    if not host:
        return None
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port is None or port == DEFAULT_PORTS[scheme] else f"{host}:{port}"
    path = parts.path.rstrip("/")
    query = f"?{parts.query}" if parts.query else ""
    return f"{netloc}{path}{query}"


def normalize_itunes_id(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value > 0 else None
    if isinstance(value, str) and is_ascii_digits(value.strip()):
        number = int(value.strip())
        return str(number) if number > 0 else None
    return None


def normalize_guid(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip().lower() or None


def entity_id_for(source_name: str, source_key: str) -> uuid.UUID:
    """Neutral id derived from the source record that created the entity; stable on replay."""
    return uuid.uuid5(DDM_ENTITY_NAMESPACE, f"{source_name}:{source_key}")
