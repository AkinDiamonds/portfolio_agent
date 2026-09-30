"""
app/rate_limit.py
-----------------
In-memory sliding-window rate limiter, keyed by client IP.

Multi-instance note: swap this dict for a Redis-backed store (e.g. with a
SCAN-based sliding window) if you scale beyond a single process.

Design summary
~~~~~~~~~~~~~~
- One deque of ``time.monotonic()`` timestamps per IP.
- On each call to ``is_allowed``:
    1. Prune timestamps older than 60 s from the front of the deque (O(1)).
    2. If ``len(timestamps) >= per_minute`` -> deny (return False).
    3. Otherwise append the current timestamp -> allow (return True).
- Thread-safety: this class is designed for use within the single-threaded
  async event loop of Uvicorn (no multi-threaded preemption). A multi-threaded
  deployment would require an explicit ``threading.Lock``.
"""

from __future__ import annotations

from collections import deque
import time


class RateLimiter:
    """Sliding-window per-IP rate limiter (in-memory, single-process only).

    Args:
        per_minute: Maximum requests allowed per IP within any rolling 60-second
            window.
        enabled: When ``False`` every call to :meth:`is_allowed` returns
            ``True`` immediately -- no logic, no state mutation, no scattered
            ``if settings.rate_limit_enabled`` checks at call sites.
    """

    _WINDOW_SECONDS: float = 60.0
    _MAX_TRACKED_IPS: int = 10_000

    def __init__(self, *, per_minute: int, enabled: bool) -> None:
        self._per_minute = per_minute
        self._enabled = enabled
        # dict[ip_str, deque[monotonic_timestamp]]
        self._hits: dict[str, deque[float]] = {}

    # ---------------------------------------------------------------------- #
    # Public API                                                              #
    # ---------------------------------------------------------------------- #

    def is_allowed(self, ip: str) -> bool:
        """Check whether *ip* is within the rate limit.

        Args:
            ip: Client IP address string (or ``"unknown"`` for non-TCP
                connections such as Unix sockets and test clients).

        Returns:
            ``True`` if the request is allowed, ``False`` if the limit is
            exceeded and the request should be rejected with HTTP 429.
        """
        if not self._enabled:
            return True

        now = time.monotonic()
        cutoff = now - self._WINDOW_SECONDS

        # Periodically evict inactive IPs if table grows large
        if len(self._hits) > self._MAX_TRACKED_IPS:
            stale_ips = [k for k, v in self._hits.items() if not v or v[-1] < cutoff]
            for k in stale_ips:
                self._hits.pop(k, None)

        timestamps = self._hits.setdefault(ip, deque())

        # Prune stale timestamps from the left (deque is chronologically ordered)
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()

        if len(timestamps) >= self._per_minute:
            return False

        timestamps.append(now)
        return True

