"""
tests/test_rate_limit.py
------------------------
Unit tests for the in-memory sliding-window RateLimiter in app/rate_limit.py.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.rate_limit import RateLimiter


class TestRateLimiter(unittest.TestCase):
    def test_allows_requests_under_limit(self) -> None:
        """Requests up to per_minute are allowed."""
        limiter = RateLimiter(per_minute=5, enabled=True)
        ip = "192.168.1.1"

        for _ in range(5):
            self.assertTrue(limiter.is_allowed(ip))

    def test_blocks_request_exceeding_limit(self) -> None:
        """The (N+1)-th request within 60 seconds is rejected."""
        limiter = RateLimiter(per_minute=3, enabled=True)
        ip = "10.0.0.1"

        self.assertTrue(limiter.is_allowed(ip))
        self.assertTrue(limiter.is_allowed(ip))
        self.assertTrue(limiter.is_allowed(ip))
        # 4th request must be rejected
        self.assertFalse(limiter.is_allowed(ip))

    def test_sliding_window_prunes_stale_timestamps(self) -> None:
        """Timestamps older than 60 seconds expire, allowing new requests."""
        limiter = RateLimiter(per_minute=2, enabled=True)
        ip = "10.0.0.2"

        with patch("time.monotonic") as mock_time:
            # T = 0.0s: 2 requests fill the limit
            mock_time.return_value = 100.0
            self.assertTrue(limiter.is_allowed(ip))
            mock_time.return_value = 110.0
            self.assertTrue(limiter.is_allowed(ip))

            # T = 120.0s: within window of both hits (120 - 100 = 20s, 120 - 110 = 10s) -> denied
            mock_time.return_value = 120.0
            self.assertFalse(limiter.is_allowed(ip))

            # T = 161.0s: first request (at 100.0s) has expired (161 - 100 = 61s > 60s)
            mock_time.return_value = 161.0
            self.assertTrue(limiter.is_allowed(ip))

            # But now we have request at 110.0 and 161.0 -> 2 requests, next fails
            mock_time.return_value = 162.0
            self.assertFalse(limiter.is_allowed(ip))

            # T = 171.0s: second request (at 110.0s) has now expired (171 - 110 = 61s > 60s)
            mock_time.return_value = 171.0
            self.assertTrue(limiter.is_allowed(ip))

    def test_per_ip_isolation(self) -> None:
        """Reaching the limit on one IP does not affect another IP."""
        limiter = RateLimiter(per_minute=2, enabled=True)
        ip_a = "1.1.1.1"
        ip_b = "2.2.2.2"

        self.assertTrue(limiter.is_allowed(ip_a))
        self.assertTrue(limiter.is_allowed(ip_a))
        self.assertFalse(limiter.is_allowed(ip_a))

        # IP B should still be allowed
        self.assertTrue(limiter.is_allowed(ip_b))
        self.assertTrue(limiter.is_allowed(ip_b))
        self.assertFalse(limiter.is_allowed(ip_b))

    def test_disabled_rate_limiter_allows_unlimited(self) -> None:
        """When enabled=False, is_allowed always returns True."""
        limiter = RateLimiter(per_minute=1, enabled=False)
        ip = "192.168.1.100"

        for _ in range(50):
            self.assertTrue(limiter.is_allowed(ip))

        # Verify no internal hits state was populated
        self.assertEqual(len(limiter._hits), 0)

    def test_unknown_ip_handling(self) -> None:
        """Non-TCP or test client 'unknown' IP is tracked properly."""
        limiter = RateLimiter(per_minute=2, enabled=True)
        self.assertTrue(limiter.is_allowed("unknown"))
        self.assertTrue(limiter.is_allowed("unknown"))
        self.assertFalse(limiter.is_allowed("unknown"))


if __name__ == "__main__":
    unittest.main()
