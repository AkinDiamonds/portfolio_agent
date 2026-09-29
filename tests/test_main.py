"""
tests/test_main.py
------------------
Integration tests for FastAPI endpoints in app/main.py.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.agent import LLMError
from app.config import Settings
from app.main import app


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

        app.state.settings = self.settings
        app.state.system_prompt = self.system_prompt
        app.state.llm_client = self.mock_llm_client
        app.state.github_tool = self.mock_github_tool

        self.client = TestClient(app, raise_server_exceptions=False)

    def test_health_endpoint(self) -> None:
        """GET /health returns 200 with status ok."""
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    @patch("app.main.handle_turn", new_callable=AsyncMock)
    def test_chat_endpoint_success(self, mock_handle_turn: AsyncMock) -> None:
        """POST /chat calls handle_turn and returns reply."""
        mock_handle_turn.return_value = "Hello from agent!"

        payload = {
            "message": "Hi there",
            "history": [
                {"role": "user", "content": "Initial"},
                {"role": "assistant", "content": "Greetings"},
            ],
        }

        response = self.client.post("/chat", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"reply": "Hello from agent!"})

        mock_handle_turn.assert_called_once()
        called_messages = mock_handle_turn.call_args.kwargs["messages"]
        self.assertEqual(len(called_messages), 4)
        self.assertEqual(called_messages[0], {"role": "system", "content": self.system_prompt})
        self.assertEqual(called_messages[1], {"role": "user", "content": "Initial"})
        self.assertEqual(called_messages[2], {"role": "assistant", "content": "Greetings"})
        self.assertEqual(called_messages[3], {"role": "user", "content": "Hi there"})

    @patch("app.main.handle_turn", new_callable=AsyncMock)
    def test_chat_endpoint_llm_error_502(self, mock_handle_turn: AsyncMock) -> None:
        """POST /chat returns 502 Bad Gateway with structured error on LLMError."""
        mock_handle_turn.side_effect = LLMError("LLM unavailable: 401 Unauthorized", recoverable=False)

        payload = {"message": "Hello"}
        response = self.client.post("/chat", json=payload)

        self.assertEqual(response.status_code, 502)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"], "LLM unavailable: 401 Unauthorized")
        self.assertFalse(data["recoverable"])

    @patch("app.main.handle_turn", new_callable=AsyncMock)
    def test_chat_endpoint_unexpected_error_500(self, mock_handle_turn: AsyncMock) -> None:
        """POST /chat returns 500 on unexpected exception."""
        mock_handle_turn.side_effect = RuntimeError("Fatal crash")

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


if __name__ == "__main__":
    unittest.main()

