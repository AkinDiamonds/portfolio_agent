"""
app/main.py
-----------
FastAPI application entry point, lifecycle management, and bare /chat & /health routes.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
import openai

from app.config import load_settings, load_system_prompt
from app.llm_client import create_llm_client
from app.models import ChatRequest, ChatResponse, ErrorResponse

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown lifecycle handler.

    Initializes application settings, system prompt, and LLM client on startup.
    Fails fast if any critical configuration is missing.
    """
    settings = load_settings()
    system_prompt = load_system_prompt()
    llm_client = create_llm_client(settings)

    app.state.settings = settings
    app.state.system_prompt = system_prompt
    app.state.llm_client = llm_client

    logger.info("Server ready")
    yield


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
    """Bare non-streaming chat endpoint.

    Assembles system prompt, conversation history, and current message,
    sends to the LLM client, and returns the plain response.
    """
    system_prompt: str = request.app.state.system_prompt
    llm_client: openai.AsyncOpenAI = request.app.state.llm_client
    model: str = request.app.state.settings.llm_model

    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt}
    ]
    for history_item in body.history:
        messages.append({"role": history_item.role, "content": history_item.content})
    messages.append({"role": "user", "content": body.message})

    try:
        completion = await llm_client.chat.completions.create(
            model=model,
            messages=messages,  # type: ignore[arg-type]
            stream=False,
        )
        reply = completion.choices[0].message.content or ""
        return ChatResponse(reply=reply)
    except openai.APIError as exc:
        err_msg = exc.message if hasattr(exc, "message") and exc.message else str(exc)
        logger.error("LLM API error during /chat: %s", err_msg)
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"error": f"LLM unavailable: {err_msg}"},
        )
    except Exception as exc:
        logger.exception("Unexpected error during /chat")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": "Internal error"},
        )
