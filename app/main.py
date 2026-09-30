"""
app/main.py
-----------
FastAPI application entry point, lifecycle management, and SSE streaming /chat & /health routes.
"""

from __future__ import annotations

import logging
import contextvars
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.types import Receive, Scope, Send

from app.agent import handle_turn
from app.budget import build_messages, estimate_tokens
from app.config import Settings, load_settings, load_system_prompt
from app.github_tool import GITHUB_TOOL_SCHEMA, GithubTool
from app.llm_client import create_llm_client
from app.models import ChatRequest, ErrorResponse
from app.rate_limit import RateLimiter

logger = logging.getLogger(__name__)

if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown lifecycle handler.

    Initializes application settings, system prompt, LLM client, and GitHub tool on startup.
    Fails fast if any critical configuration is missing.
    """
    settings = load_settings()
    system_prompt = load_system_prompt()
    logger.info("System prompt fixed cost: %d tokens", estimate_tokens(system_prompt))

    llm_client = create_llm_client(settings)
    github_tool = GithubTool(
        username=settings.github_username,
        token=settings.github_token,
    )
    rate_limiter = RateLimiter(
        per_minute=settings.rate_limit_per_minute,
        enabled=settings.rate_limit_enabled,
    )

    app.state.settings = settings
    app.state.system_prompt = system_prompt
    app.state.llm_client = llm_client
    app.state.github_tool = github_tool
    app.state.rate_limiter = rate_limiter

    logger.info(
        "Server ready — CORS origin=%s rate_limit=%s (%d req/min)",
        settings.allowed_origin,
        settings.rate_limit_enabled,
        settings.rate_limit_per_minute,
    )
    try:
        yield
    finally:
        github_tool.close()


app = FastAPI(
    title="Portfolio Agent",
    description="LLM-powered portfolio assistant API with SSE streaming",
    version="0.1.0",
    lifespan=lifespan,
)

_current_app: contextvars.ContextVar[FastAPI | None] = contextvars.ContextVar(
    "current_app", default=None
)


class AppCORSMiddleware(CORSMiddleware):
    """Dynamic CORS middleware resolving allowed origin from app.state.settings."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            token = _current_app.set(scope.get("app"))
            try:
                await super().__call__(scope, receive, send)
            finally:
                _current_app.reset(token)
            return
        await super().__call__(scope, receive, send)

    def is_allowed_origin(self, origin: str) -> bool:
        app_obj = _current_app.get()
        if app_obj and hasattr(app_obj, "state") and hasattr(app_obj.state, "settings"):
            allowed = getattr(app_obj.state.settings, "allowed_origin", None)
            if allowed and origin == allowed:
                return True
        return False


# Lock CORS to the single allowed origin configured via ALLOWED_ORIGIN.
# allow_credentials=False means cookies/auth headers are never forwarded.
app.add_middleware(
    AppCORSMiddleware,
    allow_origins=[],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type"],
    allow_credentials=False,
)


@app.get("/health", summary="Health check", tags=["System"])
async def health() -> dict[str, str]:
    """Health check endpoint.

    Returns 200 with status 'ok'. Does not call LLM or GitHub.
    """
    return {"status": "ok"}


@app.post(
    "/chat",
    response_model=None,
    responses={
        200: {
            "description": (
                "Server-Sent Events (SSE) stream of agent response. Always HTTP 200 once "
                "streaming begins; in-flight agent/LLM errors are surfaced as 'error' events in the stream."
            ),
            "content": {"text/event-stream": {}},
        },
        422: {"description": "Request validation error (FastAPI default)"},
        429: {"model": ErrorResponse, "description": "Rate limit exceeded (too many requests)"},
        500: {"model": ErrorResponse, "description": "Internal server error before stream start"},
    },
    summary="Send a message to the portfolio agent (SSE stream)",
    tags=["Chat"],
)
async def chat(body: ChatRequest, request: Request) -> StreamingResponse | JSONResponse:
    """SSE-streaming chat endpoint with agent loop.

    Assembles system prompt, conversation history, and current message within token budget,
    and returns a Server-Sent Events stream yielding status, token, done, or error events.
    Rate-limited per client IP before any LLM or GitHub call is made.
    """
    # ------------------------------------------------------------------ #
    # Rate limiting — must be first, before any upstream calls            #
    # ------------------------------------------------------------------ #
    ip: str = request.client.host if request.client else "unknown"
    rate_limiter: RateLimiter | None = getattr(request.app.state, "rate_limiter", None)
    if rate_limiter is not None and not rate_limiter.is_allowed(ip):
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"error": "Too many requests. Please wait before sending another message."},
        )

    settings: Settings = request.app.state.settings
    system_prompt: str = request.app.state.system_prompt
    llm_client = request.app.state.llm_client
    github_tool: GithubTool = request.app.state.github_tool

    try:
        messages = build_messages(
            system_prompt=system_prompt,
            tool_schema=GITHUB_TOOL_SCHEMA,
            history=body.history,
            message=body.message,
            budget=settings.effective_input_budget,
        )
    except Exception as exc:
        logger.exception("Error during message assembly in /chat: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": "Internal error"},
        )

    return StreamingResponse(
        handle_turn(
            messages=messages,
            settings=settings,
            llm_client=llm_client,
            github_tool=github_tool,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
