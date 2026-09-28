# Plan 04 — Agent Loop (Tool Decision + Non-Streaming)

## Goal
Implement the core agent decision loop in `app/agent.py`. The decide step is non-streaming; tool calls are executed; the final answer is returned as plain text (no SSE yet). This plan proves the full tool-call cycle works end to end before adding streaming complexity.

## Scope
- `app/agent.py` — the `handle_turn()` generator function and all supporting logic
- Upgrade `app/main.py` — wire `handle_turn()` into `/chat`, still returning plain JSON

## Depends On
- Plan 01 (`Settings`)
- Plan 02 (`GithubTool`, `GITHUB_TOOL_SCHEMA`)
- Plan 03 (`llm_client`, `ChatRequest`)

---

## Files to Create / Modify

| File | Action | Purpose |
|---|---|---|
| `app/agent.py` | Create | Agent loop, tool dispatch, error recovery |
| `app/main.py` | Modify | Pass tool infrastructure into the loop |

---

## Agent Loop Design (`agent.py`)

### Inputs
- `messages` — the already-assembled list (system + history + current message). Budget is not applied in this plan; that comes in Plan 05.
- `settings` — for `max_tool_iterations` and `llm_model`
- `llm_client` — the `openai.OpenAI` instance
- `github_tool` — the `GithubTool` instance

### Loop structure
Iterate up to `settings.max_tool_iterations` times:

1. Call `client.chat.completions.create` with `stream=False`, passing `GITHUB_TOOL_SCHEMA` as the tools list.
2. Inspect `choices[0].finish_reason`:
   - If not `"tool_calls"` → model wants to answer directly. Break and return the content.
   - If `"tool_calls"` → extract the first tool call (only one tool exists), call `github_tool.execute()`, append the assistant message and the tool result message to the conversation, continue.
3. After exhausting `max_tool_iterations` without a direct answer → append a system note telling the model that tool access is exhausted for this turn, then make one final non-streaming call and return that answer.

### Return type (this plan)
A plain `str` — the model's final answer. SSE wrapping is added in Plan 06.

### LLM call failure handling
One retry with a 1-second delay on `openai.APIError`. If the retry also fails, raise a custom `LLMError(message, recoverable=False)` which `main.py` catches and returns as a structured error JSON. Do not retry indefinitely.

### Malformed tool-call handling
If the LLM returns `finish_reason="tool_calls"` but the tool call JSON cannot be parsed or does not pass `GithubLookupArgs` validation:
- Log the raw tool-call content at WARNING level.
- Retry once by making a new non-streaming call to the same `messages` state but **without** the tools parameter (degrade to plain answer mode).
- If that retry also fails, raise `LLMError`.

This ensures a malformed tool call degrades gracefully rather than crashing the request.

### Tool result message format
After executing the tool, append two messages to the conversation:
1. The assistant's message object (containing the tool call, as returned by the SDK — append it directly).
2. A tool result message with `role="tool"`, the matching `tool_call_id`, and `content` set to the string returned by `github_tool.execute()`.

This matches the OpenAI tool-calling message format that all compatible backends expect.

---

## Custom Exception (`LLMError`)

Defined in `agent.py` (or a small `app/exceptions.py` if preferred).

**Fields:**
- `message: str` — human-readable description
- `recoverable: bool` — whether the client should offer a retry

Used by `main.py` to return a structured error JSON instead of an unhandled 500.

---

## `app/main.py` Changes

### App state additions
On startup (lifespan), also instantiate `GithubTool(settings.github_username, settings.github_token)` and store it on `app.state`.

### Updated `POST /chat`
1. Assemble `messages` (system + history + current message) — same as Plan 03.
2. Call `handle_turn(messages, settings, llm_client, github_tool)`.
3. Return `{"reply": result_string}`.
4. Catch `LLMError` → return `{"error": e.message, "recoverable": e.recoverable}` with status 502.
5. Catch unexpected exceptions → log at ERROR, return `{"error": "Internal error"}` with status 500.

---

## Guardrails

| Risk | Mitigation |
|---|---|
| Infinite tool-call loop | Hard cap at `max_tool_iterations`; system note appended on exhaustion |
| LLM provider timeout / error | One retry with 1-second delay, then `LLMError` propagates cleanly |
| Malformed tool-call JSON from LLM | Caught, logged, retried without tools (degrade to plain answer) |
| Tool execution raises an exception | `GithubTool.execute()` never raises (Plan 02 guarantee); tool result is always a string |
| `finish_reason` is an unexpected value | Treat anything that is not `"tool_calls"` as a direct answer — conservative default |
| Model returns no choices | `IndexError` on `choices[0]` → caught by the `openai.APIError` handler |

---

## Verification Checklist

- [ ] A question that requires no tool returns the model's answer directly (one LLM call).
- [ ] A question that triggers a tool call: status shows `"checking GitHub..."` in the log; the tool result is appended; the model produces a final answer using it.
- [ ] `MAX_TOOL_ITERATIONS=1` causes a single tool call, then the "tool budget exhausted" fallback answer.
- [ ] Simulating a malformed tool response (by temporarily patching the response) produces a graceful degraded answer, not a crash.
- [ ] With a bad API key, the route returns a `{"error": "...", "recoverable": false}` JSON — not an unhandled exception.

## Definition of Done
The full agent loop works as plain JSON (`/chat` still returns `{"reply": "..."}`). Streaming is not yet present. All error paths return structured responses. The tool-call cycle is verified against the real LLM and real GitHub account.
