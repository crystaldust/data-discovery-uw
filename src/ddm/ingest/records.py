"""Normalised source records, rejects, content hashes and input fingerprints.

Hashes are computed after normalisation, so whitespace or case noise in a source
never counts as a change. Unknown values are absent from attrs, never placeholders.
"""

import hashlib
import json
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Any

from ddm.identifiers import is_ascii_digits


@dataclass(frozen=True)
class NormalizedRecord:
    source_key: str
    display_name: str | None
    attrs: Mapping[str, Any]
    source_updated_at: datetime | None
    identifiers: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Reject:
    row_number: int
    source_key: str | None
    reason: str
    raw: Mapping[str, Any]


def make_record(
    *,
    source_key: str,
    display_name: str | None,
    attrs: Mapping[str, Any],
    source_updated_at: datetime | None,
    identifiers: tuple[tuple[str, str], ...],
) -> NormalizedRecord:
    """Build a record whose attrs are a read-only copy of the given mapping."""
    return NormalizedRecord(
        source_key=source_key,
        display_name=display_name,
        attrs=MappingProxyType(dict(attrs)),
        source_updated_at=source_updated_at,
        identifiers=identifiers,
    )


def source_key_order(source_key: str) -> tuple[int, int, str]:
    """Deterministic order: numeric keys by value first, then any other keys as text."""
    if is_ascii_digits(source_key):
        return (0, int(source_key), "")
    return (1, 0, source_key)


def _json_default(value: Any) -> Any:
    if isinstance(value, date | datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )


def content_hash(record: NormalizedRecord) -> bytes:
    body = {
        "display_name": record.display_name,
        "attrs": dict(record.attrs),
        "source_updated_at": record.source_updated_at,
    }
    return hashlib.sha256(canonical_json(body).encode()).digest()


def fingerprint_item(source_key: str, digest: bytes) -> bytes:
    """One row's contribution to an input fingerprint: its key and its content."""
    return hashlib.sha256(source_key.encode() + b"\x00" + digest).digest()


def make_reject(
    *, row_number: int, source_key: str | None, reason: str, raw: Mapping[str, Any]
) -> Reject:
    """Build a reject whose raw values are a read-only copy of the given mapping."""
    return Reject(
        row_number=row_number,
        source_key=source_key,
        reason=reason,
        raw=MappingProxyType(dict(raw)),
    )


def input_fingerprint(source_name: str, params: Mapping[str, Any], hashes: Iterable[bytes]) -> str:
    """Identify one input independent of row order; same content always gives the same value."""
    digest = hashlib.sha256()
    digest.update(source_name.encode())
    digest.update(b"\n")
    digest.update(canonical_json(dict(params)).encode())
    digest.update(b"\n")
    for item in sorted(hashes):
        digest.update(item)
    return digest.hexdigest()


def reject_digest(reject: Reject) -> bytes:
    """A reject's contribution to an input fingerprint: the raw values it was made from."""
    return hashlib.sha256(canonical_json(dict(reject.raw)).encode()).digest()


# Rejects keep untrusted source data for inspection; bound it so one feed cannot break or
# bloat the write (jsonb refuses NUL and lone surrogates).
MAX_RAW_CHARS = 2000
MAX_RAW_DEPTH = 4
MAX_RAW_ITEMS = 100


def safe_raw(value: Any, depth: int = 0) -> Any:
    """A JSON-safe, size-bounded copy of untrusted data."""
    if isinstance(value, str):
        text = value.replace("\x00", "").encode("utf-8", "replace").decode("utf-8")
        return text[:MAX_RAW_CHARS]
    if value is None or isinstance(value, bool | int | float):
        return value
    if depth >= MAX_RAW_DEPTH:
        return None
    if isinstance(value, Mapping):
        items = list(value.items())[:MAX_RAW_ITEMS]
        return {safe_raw(str(k), depth + 1): safe_raw(v, depth + 1) for k, v in items}
    if isinstance(value, list | tuple):
        return [safe_raw(v, depth + 1) for v in value[:MAX_RAW_ITEMS]]
    return None


# Code points below this are Basic Latin through Latin Extended-B.
LATIN_END = "\u0250"


def norm_title(text: str | None) -> str | None:
    """Close to the database's norm_text: case-folded, single spaces, and accents removed
    from Latin letters only. Combining marks elsewhere (Japanese dakuten, Indic signs) are
    part of the word and are kept."""
    if text is None:
        return None
    kept, after_latin = [], False
    for char in unicodedata.normalize("NFKD", text):
        if unicodedata.combining(char):
            if after_latin:
                continue
        else:
            after_latin = char < LATIN_END
        kept.append(char)
    plain = unicodedata.normalize("NFC", "".join(kept))
    return " ".join(plain.casefold().split()) or None
