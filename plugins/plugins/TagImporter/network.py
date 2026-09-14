"""Dependency-free HTTP reliability layer for Multi-Booru Tag Importer."""

from __future__ import annotations

import email.utils
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional


class CircuitOpenError(RuntimeError):
    """Raised while a host circuit breaker is open after repeated transient failures."""


@dataclass
class _HostState:
    next_allowed_at: float = 0.0
    consecutive_failures: int = 0
    circuit_open_until: float = 0.0


class HTTPClient:
    """Small urllib wrapper with per-host pacing, retries, and circuit breaking.

    The wrapper returns a normal urllib response object so existing provider parsers can
    continue using ``with client.urlopen(...) as resp``. Retryable failures are retried
    before the final urllib exception is surfaced to the caller.
    """

    RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

    def __init__(self) -> None:
        self.default_min_interval = 0.25
        self.max_retries = 2
        self.backoff_base = 0.75
        self.max_backoff = 20.0
        self.circuit_threshold = 4
        self.circuit_cooldown = 120.0
        self._states: Dict[str, _HostState] = {}
        self._host_intervals: Dict[str, float] = {}
        self._host_rate_limit_cooldowns: Dict[str, float] = {}
        self._lock = threading.Lock()

    def configure(
        self,
        *,
        default_min_interval: Optional[float] = None,
        max_retries: Optional[int] = None,
        backoff_base: Optional[float] = None,
        circuit_threshold: Optional[int] = None,
        circuit_cooldown: Optional[float] = None,
    ) -> None:
        if default_min_interval is not None:
            self.default_min_interval = max(0.0, float(default_min_interval))
        if max_retries is not None:
            self.max_retries = max(0, int(max_retries))
        if backoff_base is not None:
            self.backoff_base = max(0.05, float(backoff_base))
        if circuit_threshold is not None:
            self.circuit_threshold = max(1, int(circuit_threshold))
        if circuit_cooldown is not None:
            self.circuit_cooldown = max(1.0, float(circuit_cooldown))

    def set_host_interval(self, host: str, seconds: float) -> None:
        host = str(host or "").casefold().strip()
        if host:
            self._host_intervals[host] = max(0.0, float(seconds))

    def set_host_rate_limit_cooldown(self, host: str, seconds: float) -> None:
        """Make HTTP 429 non-blocking for a host by opening its circuit immediately.

        A value greater than zero means the first 429 is surfaced immediately instead
        of sleeping/retrying. The host circuit remains open for at least ``seconds``
        (or a longer Retry-After supplied by the server). Other hosts are unaffected.
        """
        host = str(host or "").casefold().strip()
        if host:
            self._host_rate_limit_cooldowns[host] = max(0.0, float(seconds))

    def cooldown_remaining(self, host: str) -> float:
        host = str(host or "").casefold().strip()
        if not host:
            return 0.0
        with self._lock:
            state = self._states.get(host)
            if not state:
                return 0.0
            return max(0.0, state.circuit_open_until - time.monotonic())

    def reset(self) -> None:
        with self._lock:
            self._states.clear()

    @staticmethod
    def _host_for(request: urllib.request.Request) -> str:
        return (urllib.parse.urlparse(request.full_url).hostname or "unknown").casefold()

    def _state(self, host: str) -> _HostState:
        with self._lock:
            return self._states.setdefault(host, _HostState())

    def _pace(self, host: str, override_interval: Optional[float]) -> None:
        interval = (
            max(0.0, float(override_interval))
            if override_interval is not None
            else self._host_intervals.get(host, self.default_min_interval)
        )
        if interval <= 0:
            return
        while True:
            with self._lock:
                state = self._states.setdefault(host, _HostState())
                now = time.monotonic()
                wait_for = state.next_allowed_at - now
                if wait_for <= 0:
                    state.next_allowed_at = now + interval
                    return
            time.sleep(wait_for)

    @staticmethod
    def _retry_after_seconds(exc: urllib.error.HTTPError) -> float:
        raw = exc.headers.get("Retry-After") if exc.headers else None
        if not raw:
            return 0.0
        raw = raw.strip()
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
        try:
            dt = email.utils.parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0.0

    def _backoff(self, attempt: int, retry_after: float = 0.0) -> float:
        exponential = min(self.max_backoff, self.backoff_base * (2 ** attempt))
        jitter = random.uniform(0.0, max(0.05, exponential * 0.25))
        return max(retry_after, exponential + jitter)

    def _record_success(self, host: str) -> None:
        with self._lock:
            state = self._states.setdefault(host, _HostState())
            state.consecutive_failures = 0
            state.circuit_open_until = 0.0

    def _record_retryable_failure(self, host: str) -> None:
        with self._lock:
            state = self._states.setdefault(host, _HostState())
            state.consecutive_failures += 1
            if state.consecutive_failures >= self.circuit_threshold:
                state.circuit_open_until = time.monotonic() + self.circuit_cooldown

    def _open_circuit(self, host: str, seconds: float) -> None:
        with self._lock:
            state = self._states.setdefault(host, _HostState())
            state.consecutive_failures += 1
            state.circuit_open_until = max(
                state.circuit_open_until,
                time.monotonic() + max(1.0, float(seconds)),
            )

    def _check_circuit(self, host: str) -> None:
        with self._lock:
            state = self._states.setdefault(host, _HostState())
            now = time.monotonic()
            if state.circuit_open_until > now:
                remaining = state.circuit_open_until - now
                raise CircuitOpenError(
                    f"{host} temporarily paused after repeated transient failures "
                    f"({remaining:.0f}s remaining)"
                )
            if state.circuit_open_until:
                state.circuit_open_until = 0.0
                state.consecutive_failures = 0

    def urlopen(
        self,
        request: urllib.request.Request,
        *,
        timeout: float = 30.0,
        min_interval: Optional[float] = None,
        retries: Optional[int] = None,
    ):
        host = self._host_for(request)
        attempts = self.max_retries if retries is None else max(0, int(retries))
        last_error: Optional[BaseException] = None

        for attempt in range(attempts + 1):
            self._check_circuit(host)
            self._pace(host, min_interval)
            try:
                response = urllib.request.urlopen(request, timeout=timeout)
                self._record_success(host)
                return response
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in self.RETRYABLE_STATUS:
                    raise

                # Some public APIs (notably Rule34) can ask clients to wait long
                # enough that honoring Retry-After inline would stall an entire
                # Stash bulk scan. For configured hosts, 429 therefore opens a
                # host-only circuit immediately and returns control to the caller.
                # The caller can continue with other providers and leave the image
                # pending for a later pass.
                rate_limit_cooldown = self._host_rate_limit_cooldowns.get(host, 0.0)
                if exc.code == 429 and rate_limit_cooldown > 0:
                    retry_after = self._retry_after_seconds(exc)
                    self._open_circuit(host, max(rate_limit_cooldown, retry_after))
                    raise

                self._record_retryable_failure(host)
                if attempt >= attempts:
                    raise
                delay = self._backoff(attempt, self._retry_after_seconds(exc))
                try:
                    exc.close()
                except Exception:
                    pass
                time.sleep(delay)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                self._record_retryable_failure(host)
                if attempt >= attempts:
                    raise
                time.sleep(self._backoff(attempt))

        if last_error is not None:
            raise last_error
        raise RuntimeError("HTTP request failed without an exception")


def is_retryable_exception(exc: BaseException) -> bool:
    seen = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, CircuitOpenError):
            return True
        if isinstance(current, urllib.error.HTTPError):
            return current.code in HTTPClient.RETRYABLE_STATUS
        if isinstance(current, (urllib.error.URLError, TimeoutError, OSError)):
            return True
        current = current.__cause__ or current.__context__
    return False


HTTP = HTTPClient()
