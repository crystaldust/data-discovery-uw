"""scrypt password hashes for the single demo login, stored in an environment variable.

Format: scrypt$n$r$p$salt$hash, salt and hash in URL-safe base64.
"""

import base64
import binascii
import hashlib
import hmac
import secrets

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
HASH_BYTES = 32
# Bounds on parameters read back from a stored hash, so a bad value cannot make a login
# attempt use unbounded memory or time.
MAX_N = 2**15
MAX_R = 16
MAX_P = 4
MAX_MEMORY = 64 * 1024 * 1024
MIN_PASSWORD_CHARS = 12


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=MAX_MEMORY, dklen=HASH_BYTES
    )


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _derive(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """False for a wrong password and for any malformed stored value; never raises."""
    try:
        scheme, n_text, r_text, p_text, salt_text, hash_text = stored.split("$")
        n, r, p = int(n_text), int(r_text), int(p_text)
        salt = base64.urlsafe_b64decode(salt_text)
        expected = base64.urlsafe_b64decode(hash_text)
    except (ValueError, binascii.Error, AttributeError):
        return False
    if scheme != "scrypt" or not (1 < n <= MAX_N and 0 < r <= MAX_R and 0 < p <= MAX_P):
        return False
    if len(expected) != HASH_BYTES or n & (n - 1):
        return False
    try:
        derived = _derive(password, salt, n, r, p)
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(derived, expected)
