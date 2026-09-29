"""
tests/test_agent.py
-------------------
Unit tests for the agent loop in app/agent.py.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import openai
from pydantic import SecretStr

from app.agent import LLMError, handle_turn
from app.config import Settings
from app.github_tool import GithubTool


def _create_mock_completion(
    content: str | None = None,
    finish_reason: str = "stop",
    tool_calls: list[Any] | None = None,
):
    """Helper to mock an OpenAI ChatCompletion response."""
    choice_mock = MagicMock()
    choice_mock.finish_reason = finish_reason
    choice_mock.message.content = content
    choice_mock.message.tool_calls = tool_calls

    completion_mock = MagicMock()
    completion_mock.choices = [choice_mock]
    return completion_mock


def _create_mock_tool_call(
    call_id: str,
    function_name: str,
    arguments_dict_or_str: dict[str, Any] | str,
):
    """Helper to mock a ToolCall object."""
    tc = MagicMock()
    tc.id = call_id
    tc.type = "function"
    tc.function.name = function_name
    if isinstance(arguments_dict_or_str, dict):
        tc.function.arguments = json.dumps(arguments_dict_or_str)
    else:
        tc.function.arguments = arguments_dict_or_str
    return tc


class TestAgentLoop(unittest.IsolatedAsyncioTestCase):
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
        self.mock_llm_client = MagicMock()
        self.mock_llm_client.chat = MagicMock()
        self.mock_llm_client.chat.completions = MagicMock()
        self.mock_llm_client.chat.completions.create = AsyncMock()

        self.mock_github_tool = MagicMock(spec=GithubTool)

    async def test_direct_answer_no_tools(self) -> None:
        """A question requiring no tool returns direct answer in one LLM call."""
        direct_completion = _create_mock_completion(
            content="Hello! How can I help you today?",
            finish_reason="stop",
            tool_calls=None,
        )
        self.mock_llm_client.chat.completions.create.return_value = direct_completion

        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
        ]

        reply = await handle_turn(
            messages=messages,
            settings=self.settings,
            llm_client=self.mock_llm_client,
            github_tool=self.mock_github_tool,
        )

        self.assertEqual(reply, "Hello! How can I help you today?")
        self.assertEqual(self.mock_llm_client.chat.completions.create.call_count, 1)
        self.mock_github_tool.execute.assert_not_called()

    async def test_tool_call_cycle_and_final_answer(self) -> None:
        """Tool call is executed, result appended, and model produces final answer."""
        tool_call = _create_mock_tool_call(
            call_id="call_123",
            function_name="github_lookup",
            arguments_dict_or_str={"action": "list_repos"},
        )
        tool_call_completion = _create_mock_completion(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[tool_call],
        )
        final_completion = _create_mock_completion(
            content="Here are your repositories: repo-a, repo-b.",
            finish_reason="stop",
            tool_calls=None,
        )

        self.mock_llm_client.chat.completions.create.side_effect = [
            tool_call_completion,
            final_completion,
        ]
        self.mock_github_tool.execute.return_value = "repo-a (stars: 5)\nrepo-b (stars: 2)"

        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "What repos do you have?"},
        ]

        reply = await handle_turn(
            messages=messages,
            settings=self.settings,
            llm_client=self.mock_llm_client,
            github_tool=self.mock_github_tool,
        )

        self.assertEqual(reply, "Here are your repositories: repo-a, repo-b.")
        self.assertEqual(self.mock_llm_client.chat.completions.create.call_count, 2)
        self.mock_github_tool.execute.assert_called_once_with(tool_call.function.arguments)

        # Verify second LLM call received tool result
        second_call_messages = self.mock_llm_client.chat.completions.create.call_args_list[1].kwargs["messages"]
        self.assertEqual(len(second_call_messages), 4)
        self.assertEqual(second_call_messages[2]["role"], "assistant")
        self.assertEqual(second_call_messages[2]["tool_calls"][0]["id"], "call_123")
        self.assertEqual(second_call_messages[3]["role"], "tool")
        self.assertEqual(second_call_messages[3]["tool_call_id"], "call_123")
        self.assertIn("repo-a", second_call_messages[3]["content"])

    async def test_max_tool_iterations_exhaustion(self) -> None:
        """When max_tool_iterations is reached, system note is added and final call made without tools."""
        self.settings.max_tool_iterations = 1

        tool_call = _create_mock_tool_call(
            call_id="call_999",
            function_name="github_lookup",
            arguments_dict_or_str={"action": "list_repos"},
        )
        tool_call_completion = _create_mock_completion(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[tool_call],
        )
        final_completion = _create_mock_completion(
            content="Based on available info, here is the answer.",
            finish_reason="stop",
            tool_calls=None,
        )

        self.mock_llm_client.chat.completions.create.side_effect = [
            tool_call_completion,
            final_completion,
        ]
        self.mock_github_tool.execute.return_value = "list of repos..."

        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Tell me everything"},
        ]

        reply = await handle_turn(
            messages=messages,
            settings=self.settings,
            llm_client=self.mock_llm_client,
            github_tool=self.mock_github_tool,
        )

        self.assertEqual(reply, "Based on available info, here is the answer.")
        self.assertEqual(self.mock_llm_client.chat.completions.create.call_count, 2)

        # Verify last call had no tools and had system note appended
        last_call_kwargs = self.mock_llm_client.chat.completions.create.call_args_list[-1].kwargs
        self.assertIsNone(last_call_kwargs.get("tools"))
        last_messages = last_call_kwargs["messages"]
        self.assertEqual(last_messages[-1]["role"], "system")
        self.assertIn("Tool access is exhausted", last_messages[-1]["content"])

    async def test_malformed_tool_call_graceful_degradation(self) -> None:
        """Malformed tool arguments log warning and degrade to plain answer mode."""
        malformed_tool_call = _create_mock_tool_call(
            call_id="call_bad",
            function_name="github_lookup",
            arguments_dict_or_str="INVALID_JSON_STRING{{{",
        )
        tool_call_completion = _create_mock_completion(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[malformed_tool_call],
        )
        degraded_completion = _create_mock_completion(
            content="I cannot check GitHub right now, but here is what I know.",
            finish_reason="stop",
            tool_calls=None,
        )

        self.mock_llm_client.chat.completions.create.side_effect = [
            tool_call_completion,
            degraded_completion,
        ]

        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Show my projects"},
        ]

        reply = await handle_turn(
            messages=messages,
            settings=self.settings,
            llm_client=self.mock_llm_client,
            github_tool=self.mock_github_tool,
        )

        self.assertEqual(reply, "I cannot check GitHub right now, but here is what I know.")
        # GithubTool execute was not called because args were malformed
        self.mock_github_tool.execute.assert_not_called()

        # Second call was without tools
        second_call_kwargs = self.mock_llm_client.chat.completions.create.call_args_list[1].kwargs
        self.assertIsNone(second_call_kwargs.get("tools"))

    async def test_llm_retry_on_api_error_success(self) -> None:
        """One transient APIError is retried and succeeds."""
        api_error = openai.APIError("Rate limit exceeded", request=MagicMock(), body=None)
        success_completion = _create_mock_completion(
            content="Recovered after retry!",
            finish_reason="stop",
        )

        self.mock_llm_client.chat.completions.create.side_effect = [
            api_error,
            success_completion,
        ]

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            reply = await handle_turn(
                messages=[{"role": "user", "content": "Hi"}],
                settings=self.settings,
                llm_client=self.mock_llm_client,
                github_tool=self.mock_github_tool,
            )
            mock_sleep.assert_awaited_once_with(1)

        self.assertEqual(reply, "Recovered after retry!")
        self.assertEqual(self.mock_llm_client.chat.completions.create.call_count, 2)

    async def test_llm_retry_on_api_error_failure_raises_llmerror(self) -> None:
        """Two consecutive APIErrors raise unrecoverable LLMError."""
        api_error = openai.APIError("API down", request=MagicMock(), body=None)
        self.mock_llm_client.chat.completions.create.side_effect = [
            api_error,
            api_error,
        ]

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaises(LLMError) as cm:
                await handle_turn(
                    messages=[{"role": "user", "content": "Hi"}],
                    settings=self.settings,
                    llm_client=self.mock_llm_client,
                    github_tool=self.mock_github_tool,
                )

        self.assertIn("LLM unavailable", cm.exception.message)
        self.assertFalse(cm.exception.recoverable)

    async def test_empty_choices_raises_llmerror(self) -> None:
        """LLM returning empty choices list raises LLMError."""
        empty_choices_completion = MagicMock()
        empty_choices_completion.choices = []

        self.mock_llm_client.chat.completions.create.return_value = empty_choices_completion

        with self.assertRaises(LLMError) as cm:
            await handle_turn(
                messages=[{"role": "user", "content": "Hi"}],
                settings=self.settings,
                llm_client=self.mock_llm_client,
                github_tool=self.mock_github_tool,
            )
        self.assertIn("no choices", cm.exception.message)

    async def test_malformed_tool_args_schema_validation_failure(self) -> None:
        """Valid JSON but missing required action parameters degrades gracefully."""
        # get_readme requires repo parameter
        tool_call = _create_mock_tool_call(
            call_id="call_missing_repo",
            function_name="github_lookup",
            arguments_dict_or_str={"action": "get_readme"},
        )
        tool_call_completion = _create_mock_completion(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[tool_call],
        )
        degraded_completion = _create_mock_completion(
            content="I cannot look up that repo without its name.",
            finish_reason="stop",
            tool_calls=None,
        )

        self.mock_llm_client.chat.completions.create.side_effect = [
            tool_call_completion,
            degraded_completion,
        ]

        reply = await handle_turn(
            messages=[{"role": "user", "content": "Get readme"}],
            settings=self.settings,
            llm_client=self.mock_llm_client,
            github_tool=self.mock_github_tool,
        )

        self.assertEqual(reply, "I cannot look up that repo without its name.")
        self.mock_github_tool.execute.assert_not_called()

    async def test_multiple_sequential_tool_calls(self) -> None:
        """Agent executes two tool calls in sequence before producing final answer."""
        tc1 = _create_mock_tool_call(
            call_id="call_1",
            function_name="github_lookup",
            arguments_dict_or_str={"action": "list_repos"},
        )
        tc2 = _create_mock_tool_call(
            call_id="call_2",
            function_name="github_lookup",
            arguments_dict_or_str={"action": "get_readme", "repo": "cool-app"},
        )

        comp1 = _create_mock_completion(content=None, finish_reason="tool_calls", tool_calls=[tc1])
        comp2 = _create_mock_completion(content=None, finish_reason="tool_calls", tool_calls=[tc2])
        comp3 = _create_mock_completion(content="Cool app is a web application.", finish_reason="stop")

        self.mock_llm_client.chat.completions.create.side_effect = [comp1, comp2, comp3]
        self.mock_github_tool.execute.side_effect = [
            "cool-app (stars: 10)",
            "# Cool App\nA web application",
        ]

        reply = await handle_turn(
            messages=[{"role": "user", "content": "What is cool-app?"}],
            settings=self.settings,
            llm_client=self.mock_llm_client,
            github_tool=self.mock_github_tool,
        )

        self.assertEqual(reply, "Cool app is a web application.")
        self.assertEqual(self.mock_llm_client.chat.completions.create.call_count, 3)
        self.assertEqual(self.mock_github_tool.execute.call_count, 2)

    async def test_parallel_tool_calls_in_single_turn(self) -> None:
        """Multiple tool calls in a single completion are executed and appended properly."""
        tc1 = _create_mock_tool_call(
            call_id="call_1",
            function_name="github_lookup",
            arguments_dict_or_str={"action": "list_repos"},
        )
        tc2 = _create_mock_tool_call(
            call_id="call_2",
            function_name="github_lookup",
            arguments_dict_or_str={"action": "get_profile"},
        )

        parallel_completion = _create_mock_completion(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[tc1, tc2],
        )
        final_completion = _create_mock_completion(
            content="Here are your repos and profile details.",
            finish_reason="stop",
            tool_calls=None,
        )

        self.mock_llm_client.chat.completions.create.side_effect = [
            parallel_completion,
            final_completion,
        ]
        self.mock_github_tool.execute.side_effect = [
            "repo1, repo2",
            "Profile: user123",
        ]

        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Get repos and profile"},
        ]

        reply = await handle_turn(
            messages=messages,
            settings=self.settings,
            llm_client=self.mock_llm_client,
            github_tool=self.mock_github_tool,
        )

        self.assertEqual(reply, "Here are your repos and profile details.")
        self.assertEqual(self.mock_llm_client.chat.completions.create.call_count, 2)
        self.assertEqual(self.mock_github_tool.execute.call_count, 2)

        # Check second LLM call messages
        second_call_messages = self.mock_llm_client.chat.completions.create.call_args_list[1].kwargs["messages"]
        # system + user + assistant (with 2 tool_calls) + 2 tool messages = 5 messages
        self.assertEqual(len(second_call_messages), 5)
        self.assertEqual(second_call_messages[2]["role"], "assistant")
        self.assertEqual(len(second_call_messages[2]["tool_calls"]), 2)
        self.assertEqual(second_call_messages[3]["role"], "tool")
        self.assertEqual(second_call_messages[3]["tool_call_id"], "call_1")
        self.assertEqual(second_call_messages[4]["role"], "tool")
        self.assertEqual(second_call_messages[4]["tool_call_id"], "call_2")

    async def test_unexpected_finish_reason_logs_warning_and_returns_content(self) -> None:
        """Unexpected finish_reason (e.g., length) logs warning and returns content."""
        length_completion = _create_mock_completion(
            content="Truncated output...",
            finish_reason="length",
            tool_calls=None,
        )
        self.mock_llm_client.chat.completions.create.return_value = length_completion

        with self.assertLogs("app.agent", level="WARNING") as log_cm:
            reply = await handle_turn(
                messages=[{"role": "user", "content": "Long request"}],
                settings=self.settings,
                llm_client=self.mock_llm_client,
                github_tool=self.mock_github_tool,
            )

        self.assertEqual(reply, "Truncated output...")
        self.assertTrue(any("unexpected finish_reason: length" in o for o in log_cm.output))


if __name__ == "__main__":
    unittest.main()


