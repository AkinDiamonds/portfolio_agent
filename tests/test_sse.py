"""
tests/test_sse.py
-----------------
Unit tests for SSE formatting helpers in app/sse.py.
"""

from __future__ import annotations

import json
import unittest

from app.sse import sse_done, sse_error, sse_status, sse_token


class TestSSEFormatting(unittest.TestCase):
    def test_sse_status_formatting(self) -> None:
        """sse_status produces correct SSE event and json payload."""
        output = sse_status("checking GitHub...")
        self.assertTrue(output.endswith("\n\n"))
        lines = output.strip().split("\n")
        self.assertEqual(lines[0], "event: status")
        self.assertTrue(lines[1].startswith("data: "))
        data = json.loads(lines[1][6:])
        self.assertEqual(data, {"message": "checking GitHub..."})

    def test_sse_token_formatting(self) -> None:
        """sse_token produces correct SSE event and text payload."""
        output = sse_token("Hello world!")
        self.assertTrue(output.endswith("\n\n"))
        lines = output.strip().split("\n")
        self.assertEqual(lines[0], "event: token")
        self.assertTrue(lines[1].startswith("data: "))
        data = json.loads(lines[1][6:])
        self.assertEqual(data, {"text": "Hello world!"})

    def test_sse_done_formatting(self) -> None:
        """sse_done produces correct SSE event with empty JSON payload."""
        output = sse_done()
        self.assertEqual(output, "event: done\ndata: {}\n\n")

    def test_sse_error_formatting(self) -> None:
        """sse_error produces correct SSE event with message and recoverable flag."""
        output_unrecoverable = sse_error("LLM unavailable", recoverable=False)
        self.assertTrue(output_unrecoverable.endswith("\n\n"))
        lines = output_unrecoverable.strip().split("\n")
        self.assertEqual(lines[0], "event: error")
        data = json.loads(lines[1][6:])
        self.assertEqual(data, {"message": "LLM unavailable", "recoverable": False})

        output_recoverable = sse_error("Rate limit", recoverable=True)
        lines_rec = output_recoverable.strip().split("\n")
        self.assertEqual(lines_rec[0], "event: error")
        data_rec = json.loads(lines_rec[1][6:])
        self.assertEqual(data_rec, {"message": "Rate limit", "recoverable": True})

    def test_sse_unicode_handling(self) -> None:
        """SSE formatting preserves Unicode and emojis properly."""
        output = sse_token("🚀 Python 3.12 ⚡")
        self.assertIn("🚀 Python 3.12 ⚡", output)
        data = json.loads(output.strip().split("\n")[1][6:])
        self.assertEqual(data["text"], "🚀 Python 3.12 ⚡")


if __name__ == "__main__":
    unittest.main()
