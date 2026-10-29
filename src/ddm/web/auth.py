"""The single demo login: a signed session cookie (Starlette SessionMiddleware).

Username and password hash come from the environment; there is no users table. The
cookie is SameSite=Lax, so other sites cannot POST to /login or /logout with it.
"""

import hmac
import logging
import threading
import time
from collections import deque

from fastapi import Request

from ddm.access.passwords import verify_password

logger = logging.getLogger(__name__)

SESSION_USER = "user"
SESSION_MAX_AGE_SECONDS = 8 * 60 * 60


# Failed logins per client per window. In memory, so per instance: it slows down guessing,
# while an edge rule (Vercel firewall) is the real limit. There is deliberately no global
# cap: anyone could fill it and lock the real user out.
LOGIN_WINDOW_SECONDS = 15 * 60
MAX_FAILURES_PER_CLIENT = 10


class FailureLimiter:
    def __init__(self, window: float, per_client: int) -> None:
        self._window, self._per_client = window, per_client
        self._failures: deque[tuple[float, str]] = deque()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        while self._failures and self._failures[0][0] <= now - self._window:
            self._failures.popleft()

    def blocked(self, client: str) -> bool:
        with self._lock:
            self._prune(time.monotonic())
            return sum(1 for _, c in self._failures if c == client) >= self._per_client

    def record(self, client: str) -> None:
        with self._lock:
            self._failures.append((time.monotonic(), client))
        logger.warning("step=login result=failed")


def new_login_limiter() -> FailureLimiter:
    return FailureLimiter(LOGIN_WINDOW_SECONDS, MAX_FAILURES_PER_CLIENT)


def client_address(request: Request) -> str:
    """Vercel puts the caller's address first in X-Forwarded-For (it overwrites what the
    client sent); locally there is no proxy."""
    forwarded = request.headers.get("x-forwarded-for", "")
    first = forwarded.split(",")[0].strip()
    return first or (request.client.host if request.client else "unknown")


class LoginRequired(Exception):
    """Raised by require_user; the app turns it into a redirect to /login."""


def credentials_match(request: Request, username: str, password: str) -> bool:
    config = request.app.state.config
    # Both checks always run, so timing does not tell which one failed.
    user_ok = hmac.compare_digest(username.encode(), config.demo_user.encode())
    password_ok = verify_password(password, config.demo_password_hash)
    return user_ok and password_ok


def log_in(request: Request) -> None:
    request.session.clear()
    request.session[SESSION_USER] = request.app.state.config.demo_user
    logger.info("step=login result=ok")


def log_out(request: Request) -> None:
    request.session.clear()


def require_user(request: Request) -> str:
    user = request.session.get(SESSION_USER)
    if user != request.app.state.config.demo_user:
        raise LoginRequired
    return user
