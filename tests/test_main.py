"""
tests/test_main.py
------------------
Integration tests for FastAPI endpoints in app/main.py.
"""

from __future__ import annotations

import unittest
from typing import AsyncIterator
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.main import app
from app.rate_limit import RateLimiter
from app.sse import sse_done, sse_status, sse_token


class TestMainEndpoints(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            llm_base_url="https://api.openai.com/v1",
            llm_api_key=SecretStr("sk-test-key"),
            llm_model="gpt-4o-mini",
            github_username="testuser",
            github_token="ghp_testtoken",
            allowed_origin="https://example.com",
            max_tool_iterations=3,
        )
        self.system_prompt = "You are a helpful assistant."
        self.mock_llm_client = MagicMock()
        self.mock_github_tool = MagicMock()

        self.rate_limiter = RateLimiter(per_minute=20, enabled=True)

        app.state.settings = self.settings
        app.state.system_prompt = self.system_prompt
        app.state.llm_client = self.mock_llm_client
        app.state.github_tool = self.mock_github_tool
        app.state.rate_limiter = self.rate_limiter

        self.client = TestClient(app, raise_server_exceptions=False)

    def test_health_endpoint(self) -> None:
        """GET /health returns 200 with status ok."""
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    @patch("app.main.handle_turn")
    def test_chat_endpoint_streaming_success(self, mock_handle_turn: MagicMock) -> None:
        """POST /chat calls handle_turn and returns StreamingResponse with SSE headers."""
        async def _mock_events(*args, **kwargs) -> AsyncIterator[str]:
            yield sse_status("checking GitHub...")
            yield sse_token("Hello ")
            yield sse_token("world!")
            yield sse_done()

        mock_handle_turn.side_effect = _mock_events

        payload = {
            "message": "Hi there",
            "history": [
                {"role": "user", "content": "Initial"},
                {"role": "assistant", "content": "Greetings"},
            ],
        }

        response = self.client.post("/chat", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/event-stream", response.headers["content-type"])
        self.assertEqual(response.headers["cache-control"], "no-cache")
        self.assertEqual(response.headers["x-accel-buffering"], "no")

        expected_body = (
            sse_status("checking GitHub...")
            + sse_token("Hello ")
            + sse_token("world!")
            + sse_done()
        )
        self.assertEqual(response.text, expected_body)

        mock_handle_turn.assert_called_once()
        called_messages = mock_handle_turn.call_args.kwargs["messages"]
        self.assertEqual(len(called_messages), 4)
        self.assertEqual(called_messages[0], {"role": "system", "content": self.system_prompt})
        self.assertEqual(called_messages[1], {"role": "user", "content": "Initial"})
        self.assertEqual(called_messages[2], {"role": "assistant", "content": "Greetings"})
        self.assertEqual(called_messages[3], {"role": "user", "content": "Hi there"})

    @patch("app.main.handle_turn")
    def test_chat_endpoint_budget_truncation(self, mock_handle_turn: MagicMock) -> None:
        """POST /chat truncates oldest history items when input exceeds budget."""
        async def _mock_events(*args, **kwargs) -> AsyncIterator[str]:
            yield sse_token("Trimmed reply")
            yield sse_done()

        mock_handle_turn.side_effect = _mock_events

        # Temporarily restrict max_context_tokens to a small budget
        tight_settings = Settings(
            llm_base_url="https://api.openai.com/v1",
            llm_api_key=SecretStr("sk-test-key"),
            llm_model="gpt-4o-mini",
            github_username="testuser",
            github_token="ghp_testtoken",
            allowed_origin="https://example.com",
            max_context_tokens=1_200,
            reserved_output_tokens=1_000,  # effective budget = 200 tokens
        )
        app.state.settings = tight_settings

        payload = {
            "message": "Latest question",
            "history": [
                {"role": "user", "content": "Old message " + ("x" * 400)},  # ~100 tokens
                {"role": "assistant", "content": "Old reply " + ("y" * 400)},  # ~100 tokens
                {"role": "user", "content": "Recent message"},
            ],
        }

        try:
            response = self.client.post("/chat", json=payload)
            self.assertEqual(response.status_code, 200)
            mock_handle_turn.assert_called_once()
            called_messages = mock_handle_turn.call_args.kwargs["messages"]
            # Oldest messages should be dropped, only recent message retained
            self.assertEqual(called_messages[0]["role"], "system")
            self.assertEqual(called_messages[-1]["content"], "Latest question")
            contents = [m["content"] for m in called_messages]
            self.assertFalse(
                any(c.startswith("Old message") for c in contents),
                "Oldest 'Old message' history entries should have been truncated",
            )
            self.assertFalse(
                any(c.startswith("Old reply") for c in contents),
                "Oldest 'Old reply' history entries should have been truncated",
            )
            self.assertTrue(
                any("Recent message" in c for c in contents),
                "Most recent history entry should be retained",
            )
        finally:
            app.state.settings = self.settings

    @patch("app.main.build_messages")
    def test_chat_endpoint_assembly_error_500(self, mock_build_messages: MagicMock) -> None:
        """POST /chat returns 500 JSON if error occurs before stream starts."""
        mock_build_messages.side_effect = RuntimeError("Fatal message assembly crash")

        payload = {"message": "Hello"}
        response = self.client.post("/chat", json=payload)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"error": "Internal error"})

    def test_chat_endpoint_validation_error_422(self) -> None:
        """POST /chat returns 422 on invalid payload."""
        # Empty message
        response = self.client.post("/chat", json={"message": "   "})
        self.assertEqual(response.status_code, 422)

    @patch("app.main.create_llm_client")
    @patch("app.main.load_system_prompt")
    @patch("app.main.load_settings")
    @patch("app.main.GithubTool")
    def test_lifespan_initializes_and_closes_resources(
        self,
        mock_github_tool_cls: MagicMock,
        mock_load_settings: MagicMock,
        mock_load_system_prompt: MagicMock,
        mock_create_llm_client: MagicMock,
    ) -> None:
        """Lifespan correctly initializes resources on startup and closes GithubTool on shutdown."""
        from fastapi import FastAPI
        from app.main import lifespan

        mock_load_settings.return_value = self.settings
        mock_load_system_prompt.return_value = "System prompt"
        mock_llm = MagicMock()
        mock_create_llm_client.return_value = mock_llm
        mock_gt_instance = MagicMock()
        mock_github_tool_cls.return_value = mock_gt_instance

        test_app = FastAPI(lifespan=lifespan)
        with TestClient(test_app):
            self.assertEqual(test_app.state.settings, self.settings)
            self.assertEqual(test_app.state.system_prompt, "System prompt")
            self.assertEqual(test_app.state.llm_client, mock_llm)
            self.assertEqual(test_app.state.github_tool, mock_gt_instance)

        mock_gt_instance.close.assert_called_once()
        self.assertIsNotNone(getattr(test_app.state, "rate_limiter", None))

    @patch("app.main.handle_turn")
    def test_chat_endpoint_rate_limited_429(self, mock_handle_turn: MagicMock) -> None:
        """POST /chat returns 429 when rate limit is exceeded, before stream starts."""
        # Configure a limiter with 2 requests allowed
        app.state.rate_limiter = RateLimiter(per_minute=2, enabled=True)

        async def _mock_events(*args, **kwargs) -> AsyncIterator[str]:
            yield sse_token("reply")
            yield sse_done()

        mock_handle_turn.side_effect = _mock_events
        payload = {"message": "Hello"}

        # Request 1: allowed
        res1 = self.client.post("/chat", json=payload)
        self.assertEqual(res1.status_code, 200)

        # Request 2: allowed
        res2 = self.client.post("/chat", json=payload)
        self.assertEqual(res2.status_code, 200)

        # Request 3: rate limited -> HTTP 429
        res3 = self.client.post("/chat", json=payload)
        self.assertEqual(res3.status_code, 429)
        self.assertEqual(
            res3.json(),
            {"error": "Too many requests. Please wait before sending another message."},
        )
        # handle_turn should only have been called twice, not on 3rd request
        self.assertEqual(mock_handle_turn.call_count, 2)

    def test_health_endpoint_not_rate_limited(self) -> None:
        """GET /health is never blocked by rate limiting."""
        # Limiter with 0 capacity or exhausted
        app.state.rate_limiter = RateLimiter(per_minute=1, enabled=True)
        # Exhaust limiter
        app.state.rate_limiter.is_allowed("testclient")

        # Health check must still return 200
        for _ in range(5):
            res = self.client.get("/health")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json(), {"status": "ok"})

    @patch("app.main.handle_turn")
    def test_rate_limiter_disabled_allows_unlimited(self, mock_handle_turn: MagicMock) -> None:
        """When rate limiting is disabled, requests proceed without 429."""
        app.state.rate_limiter = RateLimiter(per_minute=1, enabled=False)

        async def _mock_events(*args, **kwargs) -> AsyncIterator[str]:
            yield sse_token("reply")
            yield sse_done()

        mock_handle_turn.side_effect = _mock_events
        payload = {"message": "Hello"}

        for _ in range(10):
            res = self.client.post("/chat", json=payload)
            self.assertEqual(res.status_code, 200)

    def test_cors_matching_origin_allowed(self) -> None:
        """Requests from configured ALLOWED_ORIGIN receive CORS allow header."""
        allowed_origin = self.settings.allowed_origin
        res = self.client.get(
            "/health",
            headers={"Origin": allowed_origin},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("access-control-allow-origin"), allowed_origin)

    def test_cors_mismatching_origin_rejected(self) -> None:
        """Requests from an unauthorized origin do not receive CORS allow header."""
        res = self.client.get(
            "/health",
            headers={"Origin": "https://unauthorized-domain.com"},
        )
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.headers.get("access-control-allow-origin"))

    def test_cors_options_preflight(self) -> None:
        """OPTIONS preflight request from allowed origin returns 200 with CORS headers."""
        res = self.client.options(
            "/chat",
            headers={
                "Origin": "https://example.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type",
            },
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("access-control-allow-origin"), "https://example.com")
        self.assertIn("POST", res.headers.get("access-control-allow-methods", ""))


if __name__ == "__main__":
    unittest.main()
