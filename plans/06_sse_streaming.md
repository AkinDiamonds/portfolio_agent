# Plan 06 — SSE Streaming

## Goal
Convert the final-answer path from plain JSON to a Server-Sent Events stream. The decide + tool steps remain non-streaming. Only the model's natural-language answer is streamed token by token, which is the only part users perceive as "typing."

## Scope
- `app/sse.py` — SSE event formatting helpers
- `app/agent.py` — add streaming final-answer path
- `app/main.py` — return `StreamingResponse` from `/chat`

## Depends On
- Plan 04 (agent loop structure)
- Plan 05 (message assembly)

---

## Files to Create / Modify

| File | Action | Purpose |
|---|---|---|
| `app/sse.py` | Create | SSE event formatting helpers |
| `app/agent.py` | Modify | Yield SSE events instead of returning a string |
| `app/main.py` | Modify | Wrap agent generator in `StreamingResponse` |

---

## SSE Event Protocol

Four event types, always formatted as:

```
event: <type>\n
data: <json>\n
\n
```

| Event | Data payload | When |
|---|---|---|
| `status` | `{"message": "checking GitHub..."}` | While a tool call is in flight |
| `token` | `{"text": "<chunk>"}` | Each streamed chunk of the final answer |
| `done` | `{}` | Stream complete — client closes connection |
| `error` | `{"message": "...", "recoverable": true/false}` | Any failure — client stops and shows the message |

---

## `app/sse.py` — Design

Four plain helper functions. Each takes the relevant payload argument(s) and returns a correctly formatted SSE `str`. No classes, no state.

- `sse_status(message: str) -> str`
- `sse_token(text: str) -> str`
- `sse_done() -> str`
- `sse_error(message: str, recoverable: bool) -> str`

Each function serializes its payload dict to JSON and assembles the two-line SSE block followed by the blank-line separator. These are the only functions that touch SSE formatting — never format SSE inline elsewhere.

---

## `app/agent.py` — Changes

### `handle_turn()` becomes a generator
Convert `handle_turn()` into a generator function (using `yield`). It yields SSE-formatted strings.

**During the tool loop:**
- When a tool call is detected, yield `sse_status("checking GitHub...")`.
- When tool budget is exhausted, append the system note and proceed to the streaming call.

**Final answer — `stream_final_answer()` (private helper):**
Call `client.chat.completions.create` with `stream=True` on the current message state. Iterate over the response chunks. For each chunk that has a non-empty `choices[0].delta.content`, yield `sse_token(content)`. After the loop, yield `sse_done()`.

**On `openai.APIError` during streaming:**
- If this is the first attempt, wait 1 second and retry the streaming call once.
- If the retry also fails, yield `sse_error(str(exc), recoverable=False)`. Do not yield `sse_done()` after an error.

**On malformed tool call (from Plan 04 logic):**
- After yielding nothing (the error is internal), retry without tools as a streaming call.
- If that also fails, yield `sse_error(message, recoverable=True)`.

---

## `app/main.py` — Changes

### `POST /chat` response
Replace `{"reply": ...}` with a `fastapi.responses.StreamingResponse`:

- `media_type` must be `"text/event-stream"`.
- Set headers: `Cache-Control: no-cache`, `X-Accel-Buffering: no` (disables Nginx buffering if deployed behind one).
- The content is the `handle_turn()` generator.

### Error handling at the route level
The generator itself yields `sse_error` events internally. The route-level `try/except` only needs to catch errors that occur before the generator starts (e.g., during message assembly). If that raises, return a plain `{"error": "..."}` JSON response with status 500 — because the SSE stream hasn't started yet, a plain JSON error is acceptable and won't confuse the client.

---

## Guardrails

| Risk | Mitigation |
|---|---|
| Streaming LLM call fails mid-stream | One retry with 1-second delay; then `sse_error` event emitted; generator exits cleanly |
| Client disconnects mid-stream | FastAPI / Uvicorn handles the broken pipe; generator simply stops being consumed — no crash |
| Empty `delta.content` chunks (keep-alive or finish chunks) | Guarded with `if content` before yielding `sse_token` |
| SSE formatted incorrectly (missing blank line) | Centralized in `sse.py` helpers — formatting is never done inline |
| `done` event missing on error | Enforced: `sse_error` exits the generator without emitting `done`; client uses the `error` event as the termination signal |
| Nginx / reverse-proxy buffering swallows events | `X-Accel-Buffering: no` header set on every SSE response |

---

## Verification Checklist

Test with `curl --no-buffer` or the browser's EventSource inspector:

- [ ] A direct-answer question produces: one or more `token` events, then a `done` event. No `status` events.
- [ ] A question requiring a GitHub tool call produces: a `status` event, then `token` events, then `done`.
- [ ] Each `token` event contains valid JSON (`{"text": "..."}` with a non-empty string).
- [ ] The `done` event contains `{}` as its data payload.
- [ ] Simulating a streaming failure (bad API key mid-test) produces an `error` event with `recoverable: false` — no `done` event follows.
- [ ] `Content-Type` header on the response is `text/event-stream`.
- [ ] `Cache-Control: no-cache` is present in the response headers.

## Definition of Done
`/chat` streams token events in real time. All four SSE event types work correctly. No SSE formatting exists outside `sse.py`. The generator never raises — it always terminates with either `done` or `error`.
