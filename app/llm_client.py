"""
app/llm_client.py
-----------------
Thin factory function for instantiating the OpenAI-compatible LLM client.
"""

from __future__ import annotations

import logging
import openai
from app.config import Settings

logger = logging.getLogger(__name__)


def create_llm_client(settings: Settings) -> openai.AsyncOpenAI:
    """Create and return an async OpenAI client configured from application settings.

    Uses AsyncOpenAI so LLM calls do not block the uvicorn event loop.

    Args:
        settings: Validated application Settings containing base_url and api_key.

    Returns:
        An instantiated openai.AsyncOpenAI client ready for async completions.
    """
    logger.info(
        "Initializing LLM client — base_url=%s model=%s",
        settings.llm_base_url,
        settings.llm_model,
    )
    return openai.AsyncOpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key.get_secret_value(),
    )
