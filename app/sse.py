"""
app/sse.py
----------
Server-Sent Events (SSE) formatting helpers.

Provides formatting functions for the four SSE event types used by the
portfolio agent protocol: status, token, done, and error.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "sse_status",
    "sse_token",
    "sse_done",
    "sse_error",
]


def _format_sse(event_type: str, data: dict[str, Any]) -> str:
    """Format an SSE block with event type, JSON-encoded data, and trailing blank line."""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event_type}\ndata: {payload}\n\n"


def sse_status(message: str) -> str:
    """Format a status event emitted while a tool call is in flight.

    Args:
        message: Human-readable status description (e.g. 'checking GitHub...').

    Returns:
        Formatted SSE event string.
    """
    return _format_sse("status", {"message": message})


def sse_token(text: str) -> str:
    """Format a token event containing a streamed chunk of the final answer.

    Args:
        text: Text chunk from LLM response.

    Returns:
        Formatted SSE event string.
    """
    return _format_sse("token", {"text": text})


def sse_done() -> str:
    """Format a done event signalling the end of the SSE stream.

    Returns:
        Formatted SSE event string with empty JSON object payload.
    """
    return _format_sse("done", {})


def sse_error(message: str, recoverable: bool = False) -> str:
    """Format an error event signalling a failure during the agent turn.

    Args:
        message: Description of the error.
        recoverable: Whether client can offer a retry to the user.

    Returns:
        Formatted SSE event string.
    """
    return _format_sse("error", {"message": message, "recoverable": recoverable})
