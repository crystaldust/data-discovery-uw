"""Podcast Index API client.

Every request identifies us with the configured User-Agent. Access refusals
(401/403/429) are surfaced immediately and never retried with another identity,
per the Podcast Index Terms of Service sections 2.3 and 2.4.
"""

import hashlib
import logging
import time
from collections.abc import Callable, Mapping
from typing import Any, Self

import httpx
from tenacity import (
    RetryError,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ddm.config import PodcastIndexConfig

logger = logging.getLogger(__name__)

BASE_URL = "https://api.podcastindex.org/api/1.0"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_WAIT_SECONDS = 2.0
DEFAULT_MIN_INTERVAL_SECONDS = 1.0
ACCESS_DENIED_STATUSES = frozenset({401, 403, 429})


class SourceAccessDenied(RuntimeError):
    """The source refused access (401/403/429). Never retry with a different identity."""

    def __init__(self, status_code: int, retry_after: str | None, path: str):
        super().__init__(f"access denied status={status_code} path={path}")
        self.status_code = status_code
        self.retry_after = retry_after
        self.path = path


class SourceUnavailable(RuntimeError):
    """The source kept failing with network errors or 5xx responses."""


class SourceResponseError(RuntimeError):
    """The source answered, but not usefully: a 4xx other than a refusal, or a body that is
    not a JSON object. Retrying the same request would get the same answer."""


class _RetryableError(Exception):
    pass


def build_auth_headers(config: PodcastIndexConfig, now_epoch: int) -> dict[str, str]:
    date = str(now_epoch)
    digest = hashlib.sha1(  # noqa: S324 - signature scheme mandated by the API
        f"{config.api_key}{config.api_secret}{date}".encode()
    ).hexdigest()
    return {
        "User-Agent": config.user_agent,
        "X-Auth-Date": date,
        "X-Auth-Key": config.api_key,
        "Authorization": digest,
    }


class PodcastIndexClient:
    def __init__(
        self,
        config: PodcastIndexConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_wait_seconds: float = DEFAULT_RETRY_WAIT_SECONDS,
        min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
    ):
        self._config = config
        self._clock = clock
        self._max_attempts = max_attempts
        self._retry_wait_seconds = retry_wait_seconds
        self._min_interval_seconds = min_interval_seconds
        self._last_request_at: float | None = None
        # No proxies (an employer requirement, and the key must not pass through one) and
        # no redirects: the API has no reason to send us anywhere else.
        self._http = httpx.Client(
            base_url=BASE_URL,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        retrying = Retrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_exponential(multiplier=self._retry_wait_seconds),
            retry=retry_if_exception_type(_RetryableError),
            reraise=False,
        )
        try:
            return retrying(self._get_once, path, dict(params or {}))
        except RetryError as exc:
            raise SourceUnavailable(
                f"podcastindex unavailable after {self._max_attempts} attempts path={path}"
            ) from exc

    def _get_once(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        self._respect_min_interval()
        headers = build_auth_headers(self._config, now_epoch=int(self._clock()))
        try:
            response = self._http.get(path.lstrip("/"), params=params, headers=headers)
        except httpx.TransportError as exc:
            logger.warning("podcastindex network error path=%s error=%s", path, exc)
            raise _RetryableError(str(exc)) from exc

        logger.info("podcastindex request path=%s status=%s", path, response.status_code)
        if response.status_code in ACCESS_DENIED_STATUSES:
            raise SourceAccessDenied(
                response.status_code, response.headers.get("Retry-After"), path
            )
        if response.status_code >= 500:
            raise _RetryableError(f"status={response.status_code}")
        if response.status_code >= 300:
            raise SourceResponseError(f"status={response.status_code} path={path}")
        try:
            body = response.json()
        except ValueError as exc:
            raise SourceResponseError(f"body is not JSON path={path}") from exc
        if not isinstance(body, dict):
            raise SourceResponseError(f"body is not a JSON object path={path}")
        return body

    def _respect_min_interval(self) -> None:
        if self._last_request_at is not None and self._min_interval_seconds > 0:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self._min_interval_seconds:
                time.sleep(self._min_interval_seconds - elapsed)
        self._last_request_at = time.monotonic()
