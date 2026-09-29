"""
app/config.py
-------------
Validated application settings via pydantic-settings.

The server refuses to start if any required environment variable is missing
or malformed — no silent broken states.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import Field, SecretStr, field_validator, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """All configuration for the portfolio agent, sourced from environment
    variables (or a .env file).  Fields with no default are *required* — the
    process will exit before binding a port if any are absent or invalid.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ------------------------------------------------------------------ #
    # Required — no default → fast-fail if absent                         #
    # ------------------------------------------------------------------ #

    llm_base_url: str = Field(
        ...,
        description="Base URL of the OpenAI-compatible LLM provider (e.g. https://api.openai.com/v1).",
    )
    llm_api_key: SecretStr = Field(
        ...,
        description="API key for the LLM provider.",
    )
    llm_model: str = Field(
        ...,
        description="Model identifier to use for completions (e.g. gpt-4o-mini).",
    )
    github_username: str = Field(
        ...,
        description="GitHub username of the portfolio owner.",
    )
    github_token: str = Field(
        ...,
        description="Fine-grained GitHub PAT with public read access.",
    )
    allowed_origin: str = Field(
        ...,
        description=(
            "CORS allowed origin. Set to your real domain — never '*'. "
            "Required so deployers cannot accidentally ship an open CORS policy."
        ),
    )

    # ------------------------------------------------------------------ #
    # Optional — sensible defaults                                        #
    # ------------------------------------------------------------------ #

    max_context_tokens: int = Field(
        default=30_000,
        ge=1_000,
        description="Total token budget for the model context window.",
    )
    reserved_output_tokens: int = Field(
        default=1_000,
        ge=100,
        description="Tokens reserved for the model's response.",
    )
    max_tool_iterations: int = Field(
        default=4,
        ge=1,
        le=10,
        description="Maximum number of tool-call iterations per user turn.",
    )
    llm_max_retries: int = Field(
        default=2,
        ge=1,
        le=5,
        description="Maximum number of attempts for LLM API calls on transient errors.",
    )
    rate_limit_enabled: bool = Field(
        default=True,
        description="Enable per-IP rate limiting.",
    )
    rate_limit_per_minute: int = Field(
        default=20,
        ge=1,
        description="Maximum requests per minute per IP when rate limiting is enabled.",
    )

    # ------------------------------------------------------------------ #
    # Validators                                                          #
    # ------------------------------------------------------------------ #

    @field_validator("llm_base_url", mode="before")
    @classmethod
    def validate_llm_base_url(cls, value: str) -> str:
        """Ensure the URL is HTTP/HTTPS and strip any trailing slash."""
        if not isinstance(value, str) or not value.startswith("http"):
            raise ValueError(
                f"LLM_BASE_URL must start with 'http' or 'https', got: {value!r}"
            )
        return value.rstrip("/")

    # ------------------------------------------------------------------ #
    # Computed properties                                                 #
    # ------------------------------------------------------------------ #

    @computed_field  # type: ignore[prop-decorator]
    @property
    def effective_input_budget(self) -> int:
        """Token budget available for input (context minus reserved output).

        Used by budget.py to cap the total context fed to the model.
        Always positive because ``max_context_tokens >= 1 000`` and
        ``reserved_output_tokens >= 100``.
        """
        return self.max_context_tokens - self.reserved_output_tokens


# --------------------------------------------------------------------------- #
# Startup loaders (plain functions — not methods)                              #
# --------------------------------------------------------------------------- #


def load_settings() -> Settings:
    """Instantiate and return a validated :class:`Settings` object.

    Raises:
        pydantic.ValidationError: if any required env var is missing or a
            validator rejects a value.  The error message names every failing
            field so the operator knows exactly what to fix.
    """
    settings = Settings()  # ValidationError propagates to caller
    logger.info(
        "Settings loaded — model=%s github_user=%s budget=%d tokens",
        settings.llm_model,
        settings.github_username,
        settings.effective_input_budget,
    )
    return settings


def load_system_prompt(path: str | Path = "system_prompt.md") -> str:
    """Read and return the system prompt from *path*.

    Args:
        path: Path to the system prompt Markdown file.
              Defaults to ``system_prompt.md`` in the working directory.

    Returns:
        The raw text content of the system prompt.

    Raises:
        FileNotFoundError: if the file does not exist.
        ValueError: if the file exists but is empty (after stripping whitespace).
    """
    prompt_path = Path(path)
    if not prompt_path.exists():
        raise FileNotFoundError(
            f"System prompt file not found: {prompt_path.resolve()}"
        )

    content = prompt_path.read_text(encoding="utf-8").strip()

    if not content:
        raise ValueError(
            f"System prompt file is empty: {prompt_path.resolve()}"
        )

    logger.info(
        "System prompt loaded from '%s' — %d chars", prompt_path, len(content)
    )
    return content
