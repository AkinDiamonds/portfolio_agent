"""
app/main.py
-----------
FastAPI application entry point, lifecycle management, and non-streaming /chat & /health routes.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.agent import LLMError, handle_turn
from app.config import Settings, load_settings, load_system_prompt
from app.github_tool import GithubTool
from app.llm_client import create_llm_client
from app.models import ChatRequest, ChatResponse, ErrorResponse

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
    llm_client = create_llm_client(settings)
    github_tool = GithubTool(
        username=settings.github_username,
        token=settings.github_token,
    )

    app.state.settings = settings
    app.state.system_prompt = system_prompt
    app.state.llm_client = llm_client
    app.state.github_tool = github_tool

    logger.info("Server ready")
    try:
        yield
    finally:
        github_tool.close()


app = FastAPI(
    title="Portfolio Agent",
    description="LLM-powered portfolio assistant API",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health", summary="Health check", tags=["System"])
async def health() -> dict[str, str]:
    """Health check endpoint.

    Returns 200 with status 'ok'. Does not call LLM or GitHub.
    """
    return {"status": "ok"}


@app.post(
    "/chat",
    response_model=ChatResponse,
    responses={
        422: {"description": "Request validation error (FastAPI default)"},
        502: {"model": ErrorResponse, "description": "LLM provider unavailable or rejected the request"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
    summary="Send a message to the portfolio agent",
    tags=["Chat"],
)
async def chat(body: ChatRequest, request: Request) -> JSONResponse | ChatResponse:
    """Non-streaming chat endpoint with agent loop.

    Assembles system prompt, conversation history, and current message,
    executes the agent loop with tool support, and returns the plain JSON reply.
    """
    settings: Settings = request.app.state.settings
    system_prompt: str = request.app.state.system_prompt
    llm_client = request.app.state.llm_client
    github_tool: GithubTool = request.app.state.github_tool

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt}
    ]
    for history_item in body.history:
        messages.append({"role": history_item.role, "content": history_item.content})
    messages.append({"role": "user", "content": body.message})

    try:
        reply = await handle_turn(
            messages=messages,
            settings=settings,
            llm_client=llm_client,
            github_tool=github_tool,
        )
        return ChatResponse(reply=reply)
    except LLMError as exc:
        logger.error("LLM error during /chat: %s (recoverable=%s)", exc.message, exc.recoverable)
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"error": exc.message, "recoverable": exc.recoverable},
        )
    except Exception as exc:
        logger.exception("Unexpected error during /chat")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": "Internal error"},
        )
