# Plan 03 — LLM Client & Bare `/chat` Route

## Goal
Wire up the OpenAI-compatible LLM client and prove end-to-end connectivity with the simplest possible `/chat` route — no tools, no SSE, no budget logic. Just a plain JSON response that calls the model and returns its answer.

## Scope
- `app/llm_client.py` — thin LLM client wrapper
- `app/main.py` — FastAPI app with `/health` and a minimal `/chat` route
- `app/models.py` — Pydantic request/response models for the chat API

## Depends On
- Plan 01 (`Settings`, `load_settings`, `load_system_prompt`)

---

## Files to Create

| File | Purpose |
|---|---|
| `app/llm_client.py` | Instantiates and exposes the `openai.OpenAI` client |
| `app/main.py` | FastAPI app, lifespan, `/health`, bare `/chat` |
| `app/models.py` | `ChatRequest` and `ChatMessage` Pydantic models |

---

## `app/models.py`

### `ChatMessage`
Represents a single message in the conversation history.

**Fields:**
- `role` — `Literal["user", "assistant"]` — rejects any other role (e.g. `tool`, `system`) at the boundary. Clients cannot inject system or tool messages.
- `content` — `str`, stripped of leading/trailing whitespace, min length 1.

### `ChatRequest`
The full POST body for `/chat`.

**Fields:**
- `message` — `str`, stripped, min length 1, max length 4 000 characters. Hard cap here prevents abusively large single messages before any token budget logic runs.
- `history` — `list[ChatMessage]`, default empty list, max 100 items. The list cap is a lightweight backstop; the real trimming is done in `budget.py` (Plan 05).

**Validator:** `message` must not be blank after stripping.

---

## `app/llm_client.py`

### Design
A plain module-level factory function `create_llm_client(settings)` that instantiates `openai.OpenAI` with:
- `base_url` from `settings.llm_base_url`
- `api_key` from `settings.llm_api_key`

No subclassing, no singleton pattern, no retry logic here. Retry logic (one attempt with backoff) lives in `agent.py` (Plan 04).

The constructed client is returned and stored as an application-level dependency — initialized once in the FastAPI lifespan, attached to `app.state`.

---

## `app/main.py`

### Lifespan handler (startup/shutdown)
On startup:
1. Call `load_settings()` — raises `ValidationError` on misconfiguration, process exits.
2. Call `load_system_prompt()` — raises on missing/empty file, process exits.
3. Call `create_llm_client(settings)` — store on `app.state`.
4. Store `settings` and `system_prompt` on `app.state`.
5. Log `"Server ready"` at INFO level.

There is no teardown step needed (the `httpx.Client` in `GithubTool` is stateless enough to close naturally).

### `GET /health`
Returns `{"status": "ok"}` as a plain JSON dict. No LLM call, no GitHub call, no external I/O. Always fast, always cheap.

### `POST /chat` (bare, this plan only)
Accepts a `ChatRequest` body. Returns a plain JSON response with the model's reply.

**Request path:**
1. Validate the request body via Pydantic (`ChatRequest`) — FastAPI does this automatically.
2. Assemble the `messages` list: system message first (from `app.state.system_prompt`), then history items, then the current `message`.
3. Call `client.chat.completions.create(model=..., messages=..., stream=False)` — no tools yet.
4. Extract `choices[0].message.content` and return it as `{"reply": "..."}`.

**Error handling in this plan:**
- `openai.APIError` (and subclasses) → return a `422`-equivalent JSON error with `{"error": "LLM unavailable: <message>"}`. Do not crash the server.
- Any other unexpected exception → log at ERROR level, return `500` with `{"error": "Internal error"}`.

No retry logic in this plan — that comes in Plan 04 when the full agent loop is added.

---

## Guardrails

| Risk | Mitigation |
|---|---|
| Client sends `role: "system"` or `role: "tool"` in history | `Literal["user", "assistant"]` on `ChatMessage.role` rejects it at deserialization |
| Abusively long single message | `max_length=4000` on `ChatRequest.message` |
| Unbounded history list | `max_length=100` on `ChatRequest.history` |
| LLM provider down or unreachable | `openai.APIError` caught, returns structured error JSON — server keeps running |
| Settings or prompt missing at startup | Lifespan raises and exits — no half-started server |
| Accessing `app.state` before lifespan completes | Not possible in FastAPI's model; lifespan must finish before routes are served |

---

## Verification Checklist

Start the server: `uvicorn app.main:app --reload`

- [ ] `GET /health` returns `{"status": "ok"}` with status 200.
- [ ] `POST /chat` with a valid body returns a non-empty `{"reply": "..."}` from the model.
- [ ] `POST /chat` with `role: "system"` in history returns a 422 validation error.
- [ ] `POST /chat` with an empty `message` returns a 422 validation error.
- [ ] `POST /chat` with a `message` exceeding 4 000 chars returns a 422 validation error.
- [ ] With a bad `LLM_API_KEY`, the server starts (key isn't validated at startup), but `POST /chat` returns a structured `{"error": "..."}` — not an unhandled 500.

## Definition of Done
A running server responds to `/health` and `/chat`. The chat route calls the real model and returns its answer as plain JSON. No tools, no streaming, no budget — those come later.
