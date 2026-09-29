"""
tests/test_agent.py
-------------------
Unit tests for the agent loop and SSE streaming in app/agent.py.
"""

from __future__ import annotations

import json
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import openai
from pydantic import SecretStr

from app.agent import handle_turn
from app.config import Settings
from app.github_tool import GithubTool
from app.sse import sse_done, sse_error, sse_status, sse_token


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
    choice_mock.delta.content = content

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
        """A question requiring no tool yields token event then done event, no status events."""
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

        events = [
            event
            async for event in handle_turn(
                messages=messages,
                settings=self.settings,
                llm_client=self.mock_llm_client,
                github_tool=self.mock_github_tool,
            )
        ]

        self.assertEqual(
            events,
            [
                sse_token("Hello! How can I help you today?"),
                sse_done(),
            ],
        )
        self.assertEqual(self.mock_llm_client.chat.completions.create.call_count, 1)
        self.mock_github_tool.execute.assert_not_called()

    async def test_tool_call_cycle_and_final_answer(self) -> None:
        """Tool call yields status event, executes tool, then streams final answer."""
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

        events = [
            event
            async for event in handle_turn(
                messages=messages,
                settings=self.settings,
                llm_client=self.mock_llm_client,
                github_tool=self.mock_github_tool,
            )
        ]

        self.assertEqual(
            events,
            [
                sse_status("checking GitHub..."),
                sse_token("Here are your repositories: repo-a, repo-b."),
                sse_done(),
            ],
        )
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
        """When max_tool_iterations is reached, system note is added and final call streamed without tools."""
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

        events = [
            event
            async for event in handle_turn(
                messages=messages,
                settings=self.settings,
                llm_client=self.mock_llm_client,
                github_tool=self.mock_github_tool,
            )
        ]

        self.assertEqual(
            events,
            [
                sse_status("checking GitHub..."),
                sse_token("Based on available info, here is the answer."),
                sse_done(),
            ],
        )
        self.assertEqual(self.mock_llm_client.chat.completions.create.call_count, 2)

        # Verify last call had no tools, stream=True, and system note appended
        last_call_kwargs = self.mock_llm_client.chat.completions.create.call_args_list[-1].kwargs
        self.assertIsNone(last_call_kwargs.get("tools"))
        self.assertTrue(last_call_kwargs.get("stream"))
        last_messages = last_call_kwargs["messages"]
        self.assertEqual(last_messages[-1]["role"], "system")
        self.assertIn("Tool access is exhausted", last_messages[-1]["content"])

    async def test_malformed_tool_call_graceful_degradation(self) -> None:
        """Malformed tool arguments degrade to streaming plain answer mode without status event."""
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

        events = [
            event
            async for event in handle_turn(
                messages=messages,
                settings=self.settings,
                llm_client=self.mock_llm_client,
                github_tool=self.mock_github_tool,
            )
        ]

        self.assertEqual(
            events,
            [
                sse_token("I cannot check GitHub right now, but here is what I know."),
                sse_done(),
            ],
        )
        # Tool was not executed
        self.mock_github_tool.execute.assert_not_called()
        # Second call was with stream=True and without tools
        second_call_kwargs = self.mock_llm_client.chat.completions.create.call_args_list[1].kwargs
        self.assertIsNone(second_call_kwargs.get("tools"))
        self.assertTrue(second_call_kwargs.get("stream"))

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
            events = [
                event
                async for event in handle_turn(
                    messages=[{"role": "user", "content": "Hi"}],
                    settings=self.settings,
                    llm_client=self.mock_llm_client,
                    github_tool=self.mock_github_tool,
                )
            ]
            mock_sleep.assert_awaited_once_with(1)

        self.assertEqual(
            events,
            [
                sse_token("Recovered after retry!"),
                sse_done(),
            ],
        )
        self.assertEqual(self.mock_llm_client.chat.completions.create.call_count, 2)

    async def test_llm_retry_on_api_error_failure_yields_sse_error(self) -> None:
        """Two consecutive APIErrors yield sse_error event and no sse_done."""
        api_error = openai.APIError("API down", request=MagicMock(), body=None)
        self.mock_llm_client.chat.completions.create.side_effect = [
            api_error,
            api_error,
        ]

        with patch("asyncio.sleep", new_callable=AsyncMock):
            events = [
                event
                async for event in handle_turn(
                    messages=[{"role": "user", "content": "Hi"}],
                    settings=self.settings,
                    llm_client=self.mock_llm_client,
                    github_tool=self.mock_github_tool,
                )
            ]

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0], sse_error("LLM unavailable: API down", recoverable=False))
        self.assertNotIn(sse_done(), events)

    async def test_empty_choices_yields_sse_error(self) -> None:
        """LLM returning empty choices list yields sse_error."""
        empty_choices_completion = MagicMock()
        empty_choices_completion.choices = []

        self.mock_llm_client.chat.completions.create.return_value = empty_choices_completion

        events = [
            event
            async for event in handle_turn(
                messages=[{"role": "user", "content": "Hi"}],
                settings=self.settings,
                llm_client=self.mock_llm_client,
                github_tool=self.mock_github_tool,
            )
        ]

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0], sse_error("LLM response contained no choices", recoverable=False))

    async def test_malformed_tool_args_schema_validation_failure(self) -> None:
        """Valid JSON but missing required action parameters degrades gracefully to streaming."""
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

        events = [
            event
            async for event in handle_turn(
                messages=[{"role": "user", "content": "Get readme"}],
                settings=self.settings,
                llm_client=self.mock_llm_client,
                github_tool=self.mock_github_tool,
            )
        ]

        self.assertEqual(
            events,
            [
                sse_token("I cannot look up that repo without its name."),
                sse_done(),
            ],
        )
        self.mock_github_tool.execute.assert_not_called()

    async def test_multiple_sequential_tool_calls(self) -> None:
        """Agent executes two tool calls in sequence yielding status events before producing final answer."""
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

        events = [
            event
            async for event in handle_turn(
                messages=[{"role": "user", "content": "What is cool-app?"}],
                settings=self.settings,
                llm_client=self.mock_llm_client,
                github_tool=self.mock_github_tool,
            )
        ]

        self.assertEqual(
            events,
            [
                sse_status("checking GitHub..."),
                sse_status("checking GitHub..."),
                sse_token("Cool app is a web application."),
                sse_done(),
            ],
        )
        self.assertEqual(self.mock_llm_client.chat.completions.create.call_count, 3)
        self.assertEqual(self.mock_github_tool.execute.call_count, 2)

    async def test_parallel_tool_calls_in_single_turn(self) -> None:
        """Multiple tool calls in a single completion are executed with one status event and appended properly."""
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

        events = [
            event
            async for event in handle_turn(
                messages=messages,
                settings=self.settings,
                llm_client=self.mock_llm_client,
                github_tool=self.mock_github_tool,
            )
        ]

        self.assertEqual(
            events,
            [
                sse_status("checking GitHub..."),
                sse_token("Here are your repos and profile details."),
                sse_done(),
            ],
        )
        self.assertEqual(self.mock_llm_client.chat.completions.create.call_count, 2)
        self.assertEqual(self.mock_github_tool.execute.call_count, 2)

        # Verify second LLM call received assistant tool_calls and corresponding tool results in order
        second_call_messages = self.mock_llm_client.chat.completions.create.call_args_list[1].kwargs["messages"]
        # system + user + assistant (with 2 tool_calls) + 2 tool result messages = 5 messages
        self.assertEqual(len(second_call_messages), 5)
        self.assertEqual(second_call_messages[2]["role"], "assistant")
        self.assertEqual(len(second_call_messages[2]["tool_calls"]), 2)
        self.assertEqual(second_call_messages[3]["role"], "tool")
        self.assertEqual(second_call_messages[3]["tool_call_id"], "call_1")
        self.assertEqual(second_call_messages[4]["role"], "tool")
        self.assertEqual(second_call_messages[4]["tool_call_id"], "call_2")

    async def test_streaming_token_chunks(self) -> None:
        """Streaming response delivers multiple token chunks followed by done."""
        # Async iterator of chunks to mock real OpenAI AsyncStream chunks
        async def _mock_stream():
            for text in ["Hello", " world", "!"]:
                chunk = MagicMock()
                chunk.choices = [MagicMock()]
                chunk.choices[0].delta.content = text
                yield chunk

        self.mock_llm_client.chat.completions.create.return_value = _mock_stream()

        # Setting max_tool_iterations = 0 causes the tool loop to exit immediately
        # and invoke the final streaming response path directly
        self.settings.max_tool_iterations = 0

        events = [
            event
            async for event in handle_turn(
                messages=[{"role": "user", "content": "Hi"}],
                settings=self.settings,
                llm_client=self.mock_llm_client,
                github_tool=self.mock_github_tool,
            )
        ]

        self.assertEqual(
            events,
            [
                sse_token("Hello"),
                sse_token(" world"),
                sse_token("!"),
                sse_done(),
            ],
        )

    async def test_stream_final_answer_invalid_retry_config(self) -> None:
        """_stream_final_answer yields sse_error if max_retries <= 0."""
        from app.agent import _stream_final_answer

        events = [
            event
            async for event in _stream_final_answer(
                llm_client=self.mock_llm_client,
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "test"}],
                max_retries=0,
            )
        ]
        self.assertEqual(events, [sse_error("Invalid retry configuration", recoverable=False)])


if __name__ == "__main__":
    unittest.main()
