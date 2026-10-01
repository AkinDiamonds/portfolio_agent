"""
app/agent.py
------------
The core agent decision loop with SSE streaming.

Handles tool calling, iterative LLM turns, malformed tool call degradation,
and SSE event streaming for the portfolio agent.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

import openai
from openai.types.chat import ChatCompletion

from app.config import Settings
from app.github_tool import GITHUB_TOOL_SCHEMA, GithubLookupArgs, GithubTool
from app.sse import sse_done, sse_error, sse_status, sse_token

__all__ = [
    "handle_turn",
    "LLMError",
]

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


async def _execute_single(tc: Any, github_tool: GithubTool) -> str:
    """Execute a single GitHub tool call non-blockingly via a worker thread."""
    fn_name = getattr(getattr(tc, "function", None), "name", "github_lookup")
    logger.info("checking GitHub... (action=%s)", fn_name)
    return await asyncio.to_thread(github_tool.execute, tc.function.arguments)


async def _call_llm_with_retry(
    llm_client: openai.AsyncOpenAI,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.7,
    max_retries: int = 2,
) -> ChatCompletion:
    """Execute a non-streaming chat completion with retries on openai.APIError.

    Waits 1 second between attempts. If attempts are exhausted or choices
    are empty, raises an LLMError.
    """
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    if tools:
        kwargs["tools"] = tools

    for attempt in range(max_retries):
        try:
            completion = await llm_client.chat.completions.create(**kwargs)
            if not getattr(completion, "choices", None):
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


async def _stream_final_answer(
    llm_client: openai.AsyncOpenAI,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float = 0.7,
    max_retries: int = 2,
) -> AsyncGenerator[str, None]:
    """Stream final completion tokens from LLM, yielding SSE token events and concluding with done.

    Retries on openai.APIError with a 1-second delay.
    If retry fails, yields sse_error and terminates without sse_done.
    """
    if max_retries <= 0:
        yield sse_error("Invalid retry configuration", recoverable=False)
        return

    for attempt in range(max_retries):
        try:
            stream = await llm_client.chat.completions.create(
                model=model,
                messages=messages,
                stream=True,
                temperature=temperature,
            )

            # 1. Handle completion object with choices list (e.g. ChatCompletion or mock)
            if isinstance(getattr(stream, "choices", None), list) and stream.choices:
                choice = stream.choices[0]
                content = getattr(getattr(choice, "delta", None), "content", None) or getattr(
                    getattr(choice, "message", None), "content", None
                )
                if content:
                    yield sse_token(content)
            # 2. Async iterable stream (real AsyncOpenAI stream or async generator mock)
            elif hasattr(stream, "__aiter__"):
                async for chunk in stream:
                    if hasattr(chunk, "choices") and chunk.choices:
                        choice = chunk.choices[0]
                        delta = getattr(choice, "delta", None)
                        if delta is not None and getattr(delta, "content", None):
                            yield sse_token(delta.content)
                        elif hasattr(choice, "message") and getattr(choice.message, "content", None):
                            yield sse_token(choice.message.content)

            yield sse_done()
            return

        except openai.APIError as exc:
            err_msg = exc.message if hasattr(exc, "message") and exc.message else str(exc)
            if attempt < max_retries - 1:
                logger.warning(
                    "Streaming LLM API error on attempt %d/%d: %s. Retrying in 1s...",
                    attempt + 1,
                    max_retries,
                    err_msg,
                )
                await asyncio.sleep(1)
            else:
                logger.error(
                    "Streaming LLM API error on retry (attempt %d/%d): %s",
                    attempt + 1,
                    max_retries,
                    err_msg,
                )
                yield sse_error(f"LLM unavailable: {err_msg}", recoverable=False)
                return
        except Exception as exc:
            logger.error("Unexpected error during streaming LLM call: %s", exc, exc_info=True)
            yield sse_error(f"LLM call failed: {exc}", recoverable=False)
            return


# --------------------------------------------------------------------------- #
# Agent Loop Entry Point                                                      #
# --------------------------------------------------------------------------- #


async def handle_turn(
    messages: list[dict[str, Any]],
    settings: Settings,
    llm_client: openai.AsyncOpenAI,
    github_tool: GithubTool,
) -> AsyncGenerator[str, None]:
    """Execute the agent decision loop for a single conversation turn, yielding SSE events.

    Iteratively calls the LLM with tool definitions, executes any requested
    GitHub tool operations, yields status events, and streams the final answer
    token-by-token concluding with a done event.

    On failure, yields an sse_error event and exits cleanly without raising.

    Args:
        messages: Assembled conversation message list (system prompt + history + user message).
        settings: Application settings containing llm_model and max_tool_iterations.
        llm_client: Configured AsyncOpenAI client.
        github_tool: Configured GithubTool instance.

    Yields:
        Formatted SSE event strings (status, token, done, error).
    """
    working_messages: list[dict[str, Any]] = list(messages)
    iterations = 0
    max_retries = getattr(settings, "llm_max_retries", 2)

    try:
        while iterations < settings.max_tool_iterations:
            try:
                completion = await _call_llm_with_retry(
                    llm_client=llm_client,
                    model=settings.llm_model,
                    messages=working_messages,
                    tools=[GITHUB_TOOL_SCHEMA],
                    temperature=settings.llm_temperature,
                    max_retries=max_retries,
                )
            except LLMError as exc:
                yield sse_error(exc.message, recoverable=exc.recoverable)
                return

            choice = completion.choices[0]

            # 1. Direct answer — model did not request tool calls
            if choice.finish_reason != "tool_calls" or not choice.message.tool_calls:
                if choice.finish_reason not in ("stop", "tool_calls"):
                    logger.warning("LLM returned unexpected finish_reason: %s", choice.finish_reason)
                content = choice.message.content or ""
                if content:
                    yield sse_token(content)
                yield sse_done()
                return

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

            # Handle malformed tool calls by degrading to streaming plain answer mode without tools
            if not all_valid:
                logger.warning("Degrading to plain streaming answer mode without tools.")
                async for event in _stream_final_answer(
                    llm_client=llm_client,
                    model=settings.llm_model,
                    messages=working_messages,
                    temperature=settings.llm_temperature,
                    max_retries=max_retries,
                ):
                    yield event
                return

            # Yield status event while tool call is in flight
            yield sse_status("checking GitHub...")

            # Execute valid tool calls non-blockingly (concurrently if multiple)
            tool_results = await asyncio.gather(
                *(_execute_single(tc, github_tool) for tc in tool_calls)
            )

            # Append assistant tool call message and tool results
            # Note: content is None when the assistant emits tool calls, which is valid per OpenAI spec
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
        async for event in _stream_final_answer(
            llm_client=llm_client,
            model=settings.llm_model,
            messages=working_messages,
            max_retries=max_retries,
        ):
            yield event

    except Exception as exc:
        logger.exception("Unexpected error in agent loop: %s", exc)
        yield sse_error(f"Internal agent error: {exc}", recoverable=False)

