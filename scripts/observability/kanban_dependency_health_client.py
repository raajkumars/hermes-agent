"""Authenticated, bounded client for Hermes Kanban dependency health."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import time
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class DependencyHealthClientError(RuntimeError):
    """A dependency-health count could not be retrieved safely."""


@dataclass(frozen=True)
class _CachedCount:
    expires_at: float
    count: int


class DependencyHealthClient:
    """Read one board's content-free stranded-dependency count from Hermes API.

    The client never includes its bearer credential in exception text. Successful
    responses are cached only in-process and only for the configured TTL.
    """

    def __init__(
        self,
        *,
        api_url: str,
        bearer_credential: str,
        timeout: float = 10.0,
        cache_ttl: float = 30.0,
        max_retries: int = 2,
        retry_backoff: float = 0.5,
        opener: Callable[..., Any] = urlopen,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not api_url.strip():
            raise ValueError("api_url must not be empty")
        if not bearer_credential.strip():
            raise ValueError("bearer_credential must not be empty")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if cache_ttl < 0:
            raise ValueError("cache_ttl must not be negative")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if retry_backoff < 0:
            raise ValueError("retry_backoff must not be negative")

        self._api_url = api_url.rstrip("/")
        self._bearer_credential = bearer_credential
        self._timeout = timeout
        self._cache_ttl = cache_ttl
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._opener = opener
        self._sleeper = sleeper
        self._clock = clock
        self._cache: dict[Optional[str], _CachedCount] = {}

    def stranded_task_count(self, *, board: Optional[str] = None) -> int:
        """Return the dependency-stranded count, using a bounded TTL cache."""
        now = self._clock()
        cached = self._cache.get(board)
        if cached is not None and cached.expires_at > now:
            return cached.count

        count = self._request_count(board=board)
        if self._cache_ttl:
            self._cache[board] = _CachedCount(expires_at=now + self._cache_ttl, count=count)
        return count

    def _request_count(self, *, board: Optional[str]) -> int:
        endpoint = f"{self._api_url}/api/plugins/kanban/health/dependencies"
        if board:
            endpoint = f"{endpoint}?{urlencode({'board': board})}"
        request = Request(
            endpoint,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._bearer_credential}",
            },
        )

        for attempt in range(self._max_retries + 1):
            try:
                with self._opener(request, timeout=self._timeout) as response:
                    payload = json.load(response)
                return _parse_count(payload, endpoint)
            except HTTPError as exc:
                retry_delay = _retry_delay(exc, attempt, self._max_retries, self._retry_backoff)
                if retry_delay is None:
                    raise DependencyHealthClientError(
                        f"dependency health API returned HTTP {exc.code} for {endpoint}"
                    ) from exc
            except (URLError, OSError) as exc:
                retry_delay = _retry_delay(None, attempt, self._max_retries, self._retry_backoff)
                if retry_delay is None:
                    raise DependencyHealthClientError(f"could not reach dependency health API at {endpoint}") from exc
            except json.JSONDecodeError as exc:
                raise DependencyHealthClientError(
                    f"dependency health API returned invalid JSON for {endpoint}"
                ) from exc
            self._sleeper(retry_delay)

        raise AssertionError("retry loop must either return or raise")  # pragma: no cover


def _parse_count(payload: object, endpoint: str) -> int:
    count = payload.get("dependency_stranded_task_count") if isinstance(payload, Mapping) else None
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise DependencyHealthClientError(
            f"dependency health response from {endpoint} did not contain a non-negative integer "
            "dependency_stranded_task_count"
        )
    return count


def _retry_delay(
    error: Optional[HTTPError], attempt: int, max_retries: int, retry_backoff: float
) -> Optional[float]:
    if attempt >= max_retries:
        return None
    if error is not None and error.code not in {429, 500, 502, 503, 504}:
        return None
    if error is not None and error.code == 429:
        retry_after = error.headers.get("Retry-After") if error.headers else None
        try:
            if retry_after is not None and float(retry_after) >= 0:
                return min(float(retry_after), 60.0)
        except ValueError:
            pass
    return retry_backoff * (2**attempt)
