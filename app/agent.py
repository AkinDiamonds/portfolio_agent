"""
app/agent.py
------------
The core agent decision loop.

Handles tool calling, iterative LLM turns, malformed tool call degradation,
and error recovery for the portfolio agent.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import openai
from openai.types.chat import ChatCompletion

from app.budget import build_messages
from app.config import Settings
from app.github_tool import GITHUB_TOOL_SCHEMA, GithubLookupArgs, GithubTool

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Custom Exceptions                                                           #
# --------------------------------------------------------------------------- #


class LLMError(Exception):
    """Raised when an LLM provider call fails or cannot be recovered."""

    def __init__(self, message: str, recoverable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.recoverable = recoverable


# --------------------------------------------------------------------------- #
# Internal Helpers                                                            #
# --------------------------------------------------------------------------- #


async def _call_llm_with_retry(
    llm_client: openai.AsyncOpenAI,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    max_retries: int = 2,
) -> ChatCompletion:
    """Execute a non-streaming chat completion with retries on openai.APIError.

    Waits 1 second between attempts. If attempts are exhausted or choices
    are empty, raises an LLMError.
    """
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    if tools:
        kwargs["tools"] = tools

    for attempt in range(max_retries):
        try:
            completion = await llm_client.chat.completions.create(**kwargs)
            if not completion.choices:
                raise LLMError("LLM response contained no choices", recoverable=False)
            return completion
        except openai.APIError as exc:
            err_msg = exc.message if hasattr(exc, "message") and exc.message else str(exc)
            if attempt < max_retries - 1:
                logger.warning(
                    "LLM API error on attempt %d/%d: %s. Retrying in 1s...",
                    attempt + 1,
                    max_retries,
                    err_msg,
                )
                await asyncio.sleep(1)
            else:
                logger.error(
                    "LLM API error on retry (attempt %d/%d): %s",
                    attempt + 1,
                    max_retries,
                    err_msg,
                )
                raise LLMError(f"LLM unavailable: {err_msg}", recoverable=False) from exc
        except LLMError:
            raise
        except Exception as exc:
            logger.error("Unexpected error during LLM call: %s", exc, exc_info=True)
            raise LLMError(f"LLM call failed: {exc}", recoverable=False) from exc

    # Fallback in unexpected control flow
    raise LLMError("LLM call failed after retries", recoverable=False)


# --------------------------------------------------------------------------- #
# Agent Loop Entry Point                                                      #
# --------------------------------------------------------------------------- #


async def handle_turn(
    messages: list[dict[str, Any]],
    settings: Settings,
    llm_client: openai.AsyncOpenAI,
    github_tool: GithubTool,
) -> str:
    """Execute the agent decision loop for a single conversation turn.

    Iteratively calls the LLM with tool definitions, executes any requested
    GitHub tool operations, and appends the outputs to the message history until
    the model produces a final text answer or max tool iterations is reached.

    Args:
        messages: Assembled conversation message list (system prompt + history + user message).
        settings: Application settings containing llm_model and max_tool_iterations.
        llm_client: Configured AsyncOpenAI client.
        github_tool: Configured GithubTool instance.

    Returns:
        The final text reply from the model.

    Raises:
        LLMError: If LLM calls fail unrecoverably or API errors persist.
    """
    working_messages: list[dict[str, Any]] = list(messages)
    iterations = 0
    max_retries = getattr(settings, "llm_max_retries", 2)

    while iterations < settings.max_tool_iterations:
        completion = await _call_llm_with_retry(
            llm_client=llm_client,
            model=settings.llm_model,
            messages=working_messages,
            tools=[GITHUB_TOOL_SCHEMA],
            max_retries=max_retries,
        )

        choice = completion.choices[0]

        # 1. Direct answer — model did not request tool calls
        if choice.finish_reason != "tool_calls" or not choice.message.tool_calls:
            if choice.finish_reason not in ("stop", "tool_calls"):
                logger.warning("LLM returned unexpected finish_reason: %s", choice.finish_reason)
            return choice.message.content or ""

        # 2. Tool calls requested (handle single or parallel tool calls)
        tool_calls = choice.message.tool_calls

        # Validate each tool call against arguments schema
        all_valid = True
        for tc in tool_calls:
            try:
                raw_args = tc.function.arguments
                if isinstance(raw_args, str):
                    parsed = json.loads(raw_args)
                elif isinstance(raw_args, dict):
                    parsed = raw_args
                else:
                    parsed = {}
                GithubLookupArgs.model_validate(parsed)
            except Exception as val_exc:
                all_valid = False
                logger.warning(
                    "Malformed tool call from LLM (id=%s, fn=%s, args=%r): %s",
                    getattr(tc, "id", "unknown"),
                    getattr(getattr(tc, "function", None), "name", "unknown"),
                    getattr(getattr(tc, "function", None), "arguments", ""),
                    val_exc,
                )

        # Handle malformed tool calls by degrading to plain answer mode
        if not all_valid:
            logger.warning("Degrading to plain answer mode without tools.")
            degraded_completion = await _call_llm_with_retry(
                llm_client=llm_client,
                model=settings.llm_model,
                messages=working_messages,
                tools=None,
                max_retries=max_retries,
            )
            return degraded_completion.choices[0].message.content or ""

        # Execute valid tool calls non-blockingly (concurrently if multiple)
        async def _execute_single(tc: Any) -> str:
            fn_name = getattr(getattr(tc, "function", None), "name", "github_lookup")
            logger.info("checking GitHub... (action=%s)", fn_name)
            return await asyncio.to_thread(github_tool.execute, tc.function.arguments)

        tool_results = await asyncio.gather(*(_execute_single(tc) for tc in tool_calls))

        # Append assistant tool call message and tool results
        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": choice.message.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in tool_calls
            ],
        }
        working_messages.append(assistant_msg)
        for tc, res in zip(tool_calls, tool_results):
            working_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": res,
                }
            )

        iterations += 1

    # 3. Exhausted tool iterations safety valve
    logger.info(
        "Reached max tool iterations (%d). Forcing final answer without tools.",
        settings.max_tool_iterations,
    )
    working_messages.append(
        {
            "role": "system",
            "content": (
                "Tool access is exhausted for this turn. Please provide your final answer "
                "to the user based on the information gathered so far without making further tool calls."
            ),
        }
    )
    final_completion = await _call_llm_with_retry(
        llm_client=llm_client,
        model=settings.llm_model,
        messages=working_messages,
        tools=None,
        max_retries=max_retries,
    )
    return final_completion.choices[0].message.content or ""
