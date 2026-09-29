"""
tests/test_budget.py
--------------------
Unit tests for token estimation and message truncation in app/budget.py.
"""

from __future__ import annotations

import logging
import unittest
from typing import Any

from app.budget import (
    MESSAGE_OVERHEAD_TOKENS,
    build_messages,
    estimate_message_tokens,
    estimate_tokens,
)
from app.models import ChatMessage


class TestTokenEstimation(unittest.TestCase):
    def test_estimate_tokens_empty_string(self) -> None:
        """estimate_tokens('') returns at least 1, never zero."""
        self.assertEqual(estimate_tokens(""), 1)

    def test_estimate_tokens_short_string(self) -> None:
        """Short strings with len < 4 return at least 1."""
        self.assertEqual(estimate_tokens("a"), 1)
        self.assertEqual(estimate_tokens("abc"), 1)

    def test_estimate_tokens_exact_and_long_strings(self) -> None:
        """len(text) // 4 heuristic calculation."""
        self.assertEqual(estimate_tokens("1234"), 1)
        self.assertEqual(estimate_tokens("12345678"), 2)
        self.assertEqual(estimate_tokens("a" * 100), 25)

    def test_estimate_message_tokens_dict(self) -> None:
        """Message dict token estimate sums role + content lengths // 4 + overhead."""
        msg = {"role": "user", "content": "12345678"}  # role=4, content=8 -> (12 // 4) + 4 = 7
        expected = (len("user") + len("12345678")) // 4 + MESSAGE_OVERHEAD_TOKENS
        self.assertEqual(estimate_message_tokens(msg), expected)

    def test_estimate_message_tokens_object(self) -> None:
        """Message object (like ChatMessage) is supported."""
        chat_msg = ChatMessage(role="user", content="Hello world")
        expected = (len("user") + len("Hello world")) // 4 + MESSAGE_OVERHEAD_TOKENS
        self.assertEqual(estimate_message_tokens(chat_msg), expected)

    def test_estimate_message_tokens_empty_content(self) -> None:
        """Empty or None content handled gracefully."""
        msg = {"role": "assistant", "content": ""}
        expected = (len("assistant") + len("")) // 4 + MESSAGE_OVERHEAD_TOKENS
        self.assertEqual(estimate_message_tokens(msg), expected)


class TestBuildMessages(unittest.TestCase):
    def setUp(self) -> None:
        self.system_prompt = "You are a helpful assistant."
        self.tool_schema = {
            "type": "function",
            "function": {
                "name": "github_lookup",
                "description": "Lookup GitHub data",
            },
        }

    def test_empty_history(self) -> None:
        """With empty history, returns [system_msg, user_msg]."""
        messages = build_messages(
            system_prompt=self.system_prompt,
            tool_schema=self.tool_schema,
            history=[],
            message="Hello",
            budget=10_000,
        )
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0], {"role": "system", "content": self.system_prompt})
        self.assertEqual(messages[1], {"role": "user", "content": "Hello"})

    def test_history_fits_entirely(self) -> None:
        """A history that fits entirely within the budget is returned unchanged (no drops)."""
        history = [
            {"role": "user", "content": "What is Python?"},
            {"role": "assistant", "content": "A programming language."},
            {"role": "user", "content": "Tell me more."},
            {"role": "assistant", "content": "It is widely used in AI and web dev."},
        ]

        messages = build_messages(
            system_prompt=self.system_prompt,
            tool_schema=self.tool_schema,
            history=history,
            message="Thank you",
            budget=10_000,
        )

        self.assertEqual(len(messages), 6)
        self.assertEqual(messages[0], {"role": "system", "content": self.system_prompt})
        self.assertEqual(messages[1:5], history)
        self.assertEqual(messages[5], {"role": "user", "content": "Thank you"})

    def test_history_with_chat_message_models(self) -> None:
        """History containing Pydantic ChatMessage models is normalized and preserved."""
        history = [
            ChatMessage(role="user", content="Hi"),
            ChatMessage(role="assistant", content="Hello"),
        ]

        messages = build_messages(
            system_prompt=self.system_prompt,
            tool_schema=None,
            history=history,
            message="How are you?",
            budget=5_000,
        )

        self.assertEqual(len(messages), 4)
        self.assertEqual(messages[0], {"role": "system", "content": self.system_prompt})
        self.assertEqual(messages[1], {"role": "user", "content": "Hi"})
        self.assertEqual(messages[2], {"role": "assistant", "content": "Hello"})
        self.assertEqual(messages[3], {"role": "user", "content": "How are you?"})

    def test_oldest_turns_dropped_when_budget_exceeded(self) -> None:
        """A history exceeding budget drops the oldest turns until the newest fit."""
        sys_msg = {"role": "system", "content": self.system_prompt}
        user_msg = {"role": "user", "content": "Current question"}

        # Calculate exact fixed costs without tool schema
        fixed_costs = estimate_message_tokens(sys_msg) + estimate_message_tokens(user_msg)

        msg1 = {"role": "user", "content": "A" * 40}  # ~14 tokens
        msg2 = {"role": "assistant", "content": "B" * 40}  # ~14 tokens
        msg3 = {"role": "user", "content": "C" * 40}  # ~14 tokens
        msg4 = {"role": "assistant", "content": "D" * 40}  # ~14 tokens

        cost4 = estimate_message_tokens(msg4)
        cost3 = estimate_message_tokens(msg3)

        # Allow budget for fixed costs + msg4 + msg3 only
        exact_budget = fixed_costs + cost4 + cost3

        history = [msg1, msg2, msg3, msg4]

        with self.assertLogs("app.budget", level="INFO") as log_cm:
            messages = build_messages(
                system_prompt=self.system_prompt,
                tool_schema=None,
                history=history,
                message="Current question",
                budget=exact_budget,
            )

        # msg1 and msg2 dropped (2 messages dropped)
        self.assertEqual(len(messages), 4)
        self.assertEqual(messages[0], sys_msg)
        self.assertEqual(messages[1], msg3)
        self.assertEqual(messages[2], msg4)
        self.assertEqual(messages[3], user_msg)

        # Check INFO log
        self.assertTrue(any("Truncated 2 history messages" in output for output in log_cm.output))

    def test_fixed_costs_exceed_budget_logs_warning_and_returns_empty_history(self) -> None:
        """When system prompt and fixed costs alone exceed budget, log WARNING and return [sys, user]."""
        history = [
            {"role": "user", "content": "Old message 1"},
            {"role": "assistant", "content": "Old message 2"},
        ]

        # Budget of 5 tokens is less than fixed costs of system prompt + user msg
        with self.assertLogs("app.budget", level="WARNING") as log_cm:
            messages = build_messages(
                system_prompt=self.system_prompt,
                tool_schema=self.tool_schema,
                history=history,
                message="Hello",
                budget=5,
            )

        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0], {"role": "system", "content": self.system_prompt})
        self.assertEqual(messages[1], {"role": "user", "content": "Hello"})
        self.assertTrue(any("Fixed token costs" in output and "exceed or equal" in output for output in log_cm.output))

    def test_ordering_guarantee(self) -> None:
        """The returned list always starts with system message and ends with user message in chronological order."""
        history = [
            {"role": "user", "content": f"msg-{i}"} for i in range(10)
        ]
        messages = build_messages(
            system_prompt="system",
            tool_schema=None,
            history=history,
            message="final user query",
            budget=10_000,
        )

        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[-1]["role"], "user")
        self.assertEqual(messages[-1]["content"], "final user query")
        # Ensure chronological ordering of retained history
        retained_contents = [m["content"] for m in messages[1:-1]]
        self.assertEqual(retained_contents, [f"msg-{i}" for i in range(10)])


if __name__ == "__main__":
    unittest.main()
