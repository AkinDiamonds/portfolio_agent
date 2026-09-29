"""
app/budget.py
-------------
Token estimation and message history truncation to fit the context window ceiling.

This module contains pure logic with no I/O: it estimates token costs for
messages and schemas and trims conversation histories (oldest first) to ensure
the assembled payload stays within the effective input budget.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Fixed overhead per message dict to account for chat ML envelope tokens.
# Based on OpenAI's chat format: <|im_start|> + role + newline + <|im_end|> = 4 tokens.
MESSAGE_OVERHEAD_TOKENS: int = 4


def estimate_tokens(text: str) -> int:
    """Estimate token count for a string using a character heuristic (len(text) // 4).

    Returns at least 1 to avoid treating any text (even empty) as 0 tokens.

    NOTE: This is a rough character-based approximation. It may under-count by
    20-50%+ for CJK, emoji, or heavily punctuated text. Do not use for exact
    billing or hard context limits.
    """
    return max(1, len(text) // 4)


def estimate_message_tokens(message: dict[str, Any] | Any) -> int:
    """Estimate the token count of a single message dictionary or object.

    Sums the role and content lengths, divides by 4, and adds a small fixed overhead
    for the message envelope.
    """
    if isinstance(message, dict):
        role = str(message.get("role", "") or "")
        content = str(message.get("content", "") or "")
    else:
        role = str(getattr(message, "role", "") or "")
        content = str(getattr(message, "content", "") or "")

    return (len(role) + len(content)) // 4 + MESSAGE_OVERHEAD_TOKENS


def build_messages(
    system_prompt: str,
    tool_schema: dict[str, Any] | list[Any] | None,
    history: list[dict[str, Any] | Any],
    message: str,
    budget: int,
) -> list[dict[str, Any]]:
    """Assemble and trim conversation messages to fit within the input token budget.

    Computes fixed costs for the system prompt, tool schema, and current user message.
    If remaining budget permits, includes prior history by greedily walking in reverse
    (newest turns first). Stops at the first turn that would exceed the remaining budget;
    all older turns beyond that point are dropped even if individually smaller (greedy
    sliding-window strategy, not global-optimal bin packing).

    Args:
        system_prompt: The fixed system prompt text.
        tool_schema: The tool schema definition (dict, list, or None) serialized for cost estimation.
        history: Conversation history list (oldest first).
        message: The current user message string.
        budget: The effective input token budget (e.g. settings.effective_input_budget).

    Returns:
        The assembled list of message dicts [system_msg, *trimmed_history, user_msg].
    """
    system_msg: dict[str, Any] = {"role": "system", "content": system_prompt}
    user_msg: dict[str, Any] = {"role": "user", "content": message}

    # Tool schema is sent as tool definition metadata, not a chat message envelope
    tool_tokens = (
        estimate_tokens(json.dumps(tool_schema)) if tool_schema is not None else 0
    )
    system_tokens = estimate_message_tokens(system_msg)
    user_tokens = estimate_message_tokens(user_msg)

    fixed_costs = system_tokens + tool_tokens + user_tokens
    remaining_budget = budget - fixed_costs

    if remaining_budget <= 0:
        logger.warning(
            "Fixed token costs (%d) exceed or equal the budget (%d). Truncating all history.",
            fixed_costs,
            budget,
        )
        return [system_msg, user_msg]

    accumulated_history: list[dict[str, Any]] = []
    for item in reversed(history):
        if isinstance(item, dict):
            msg_dict: dict[str, Any] = {
                "role": item.get("role", ""),
                "content": item.get("content", ""),
            }
        else:
            msg_dict = {
                "role": getattr(item, "role", ""),
                "content": getattr(item, "content", ""),
            }

        cost = estimate_message_tokens(msg_dict)
        if remaining_budget >= cost:
            accumulated_history.append(msg_dict)
            remaining_budget -= cost
        else:
            break

    trimmed_history = list(reversed(accumulated_history))
    dropped_count = len(history) - len(trimmed_history)
    if dropped_count > 0:
        logger.info(
            "Truncated %d history messages to fit token budget (budget=%d, remaining_tokens=%d)",
            dropped_count,
            budget,
            remaining_budget,
        )

    return [system_msg, *trimmed_history, user_msg]
