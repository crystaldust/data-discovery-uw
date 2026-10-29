"""Masks email addresses that turn up inside free text: a show description that says
"pitch us at ...", an author field that is an address, a URL with user:password@ or
?email=. Applied to every name and attribute value the query layer returns, so no
surface needs to remember it. Same masking as the database's mask_email().
"""

import re
from collections.abc import Mapping
from typing import Any

SHORT_LOCAL_PART = 3
EMAIL = re.compile(r"(?<![\w.%+-])([\w.%+-]+)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
URL_USERINFO = re.compile(r"(?i)\b(https?://)[^/\s?#@]+@")


def mask_address(local: str, domain: str) -> str:
    """A short local part would be all but given away by its first character."""
    shown = "" if len(local) <= SHORT_LOCAL_PART else local[0]
    return f"{shown}***@{domain}"


def scrub_text(text: str) -> str:
    without_userinfo = URL_USERINFO.sub(r"\1", text)
    return EMAIL.sub(lambda m: mask_address(m.group(1), m.group(2)), without_userinfo)


def scrub(value: Any) -> Any:
    """The value with every string in it scrubbed; containers are rebuilt, not changed."""
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, Mapping):
        return {k: scrub(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(scrub(v) for v in value)
    return value
