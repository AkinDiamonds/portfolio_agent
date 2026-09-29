"""
app/models.py
-------------
Pydantic request and response models for the Portfolio Agent API.
"""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator


class ChatMessage(BaseModel):
    """A single message in the conversation history.

    Rejects any role other than 'user' or 'assistant' to prevent boundary
    injections (e.g. clients supplying system or tool messages).
    """

    role: Literal["user", "assistant"] = Field(
        ...,
        description="The sender of the message. Only 'user' and 'assistant' are permitted.",
    )
    content: str = Field(
        ...,
        min_length=1,
        description="The content of the message. Must not be empty or whitespace-only.",
    )

    @field_validator("content", mode="before")
    @classmethod
    def strip_content(cls, value: Any) -> Any:
        """Strip leading/trailing whitespace before validation."""
        if isinstance(value, str):
            return value.strip()
        return value


class ChatRequest(BaseModel):
    """The request payload for the /chat endpoint."""

    message: str = Field(
        ...,
        min_length=1,
        max_length=4_000,
        description="The user's current input message. Capped at 4 000 characters.",
    )
    history: list[ChatMessage] = Field(
        default_factory=list,
        max_length=100,
        description="Prior conversation history. Capped at 100 messages.",
    )

    @field_validator("message", mode="before")
    @classmethod
    def strip_message(cls, value: Any) -> Any:
        """Strip leading/trailing whitespace before validation."""
        if isinstance(value, str):
            return value.strip()
        return value


class ChatResponse(BaseModel):
    """The response payload for a non-streaming chat turn."""

    reply: str = Field(
        ...,
        description="The assistant's reply text.",
    )


class ErrorResponse(BaseModel):
    """Structured error response payload."""

    error: str = Field(
        ...,
        description="Description of the error that occurred.",
    )
