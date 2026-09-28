# Portfolio Agent — Technical Build Spec

## 1. Context & Goal

An open-source, self-hostable Python backend that powers a chat agent for a developer portfolio site. It has exactly **one tool** — a read-only GitHub lookup tool — and is provider-agnostic on the LLM side: anyone forking the repo swaps in their own model provider, API key, GitHub username, and system prompt purely through `.env` and a prompt file, with zero code changes.

**Distribution model:** open-source template repo. Each adopter deploys their own instance. There is no shared multi-tenant service, no cross-user API keys, no per-request tenant config — all config is per-deployment via environment variables. This eliminates most of the abuse-surface/multi-tenancy concerns a shared service would have.

**Frontend for the reference deployment:** Next.js, calling this service directly over HTTPS (CORS-enabled), using Microsoft's `@microsoft/fetch-event-source` to consume the SSE stream (needed because native `EventSource` can't POST a body). Firebase is assumed to be the portfolio's existing backend for unrelated concerns (hosting, forms, etc.) and is **not** in this agent's request path — the Next.js client talks straight to this Python service's URL. *(Flag if this assumption is wrong — e.g. if you actually want to proxy through a Firebase Cloud Function for same-origin requests; that's a small addition, not a redesign.)*

## 2. Non-goals (v1)

- No conversation persistence / database. Fully stateless — the client resends history each turn.
- No write access to GitHub, ever.
- No support for non-OpenAI-compatible provider SDKs (no native Anthropic Messages API, no native Gemini SDK). If it doesn't speak OpenAI's `chat/completions` + `tools` schema at some `base_url`, it's out of scope.
- No multi-instance/shared-state deployment (no Redis). Single process is the assumed deployment shape.
- No summarization-based context compression. Truncation only (see §7).

## 3. Tech Stack

| Concern | Choice | Why |
|---|---|---|
| Web framework | FastAPI + Uvicorn | async-native, first-class SSE support, minimal boilerplate |
| LLM client | Official `openai` Python SDK, pointed at a configurable `base_url` | every target provider (OpenRouter, Groq, Together, Fireworks, DeepSeek, self-hosted vLLM/Ollama/LM Studio, Muse Glimmer) is OpenAI-compatible already; `base_url` is the built-in universal adapter. **Do not add LiteLLM or any other abstraction library** — it would translate a format that's already uniform, for no benefit, and is one more dependency that can break. |
| GitHub access | `httpx` direct calls to the GitHub REST API (not PyGithub) | keeps the dependency footprint small; we only need ~6 narrow, allow-listed read endpoints, not a full SDK |
| Config | `pydantic-settings` reading `.env` | validated, typed config; fails fast on missing/malformed values |
| Rate limiting | Simple in-memory sliding-window per-IP limiter (no external dependency) | single-process deployment assumption; swap for Redis-backed only if the deployment model changes later |

## 4. Repo Layout

```
.
├── app/
│   ├── main.py            # FastAPI app, CORS, /chat and /health routes
│   ├── config.py          # pydantic-settings model, loads & validates .env
│   ├── llm_client.py      # thin wrapper: openai.OpenAI(base_url=..., api_key=...)
│   ├── agent.py           # the decide → tool → stream-final-answer loop
│   ├── github_tool.py     # the one tool: allow-listed actions, output shaping/truncation
│   ├── budget.py          # token estimation + history truncation
│   ├── rate_limit.py      # in-memory sliding-window limiter
│   └── sse.py             # SSE event formatting helpers
├── system_prompt.md        # the only other file a forker edits
├── .env.example
├── Dockerfile
├── requirements.txt
└── README.md                # fork instructions: edit .env + system_prompt.md, deploy
```

## 5. Configuration Contract

### `.env.example`
```bash
# --- LLM provider (must be OpenAI-compatible) ---
LLM_BASE_URL=https://your-provider.example.com/v1
LLM_API_KEY=sk-...
LLM_MODEL=meta/muse-glimmer-30b

# --- Context budget ---
# Combined input+output token ceiling for a single model call.
MAX_CONTEXT_TOKENS=30000
# Tokens reserved for the model's own reply; subtracted from MAX_CONTEXT_TOKENS
# before building the prompt.
RESERVED_OUTPUT_TOKENS=1000

# --- GitHub (read-only, public data only) ---
GITHUB_USERNAME=your-github-handle
GITHUB_TOKEN=github_pat_...        # fine-grained PAT, no permissions needed beyond public read

# --- Agent behavior ---
MAX_TOOL_ITERATIONS=4              # hard cap on tool-call rounds per turn (loop guard)

# --- Networking / abuse controls ---
ALLOWED_ORIGIN=https://yourportfolio.com   # CORS; use your real domain, not *
RATE_LIMIT_ENABLED=true
RATE_LIMIT_PER_MINUTE=20
```

### `system_prompt.md`
Plain markdown/text, loaded at startup and used as the `system` message. This is the *only* other file a forker is expected to touch. No templating engine needed — read it as a raw string.

**Validation on startup:** if `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, or `GITHUB_USERNAME` are missing, fail fast with a clear error rather than starting in a broken state.

## 6. API Contract

### `POST /chat`

Request:
```json
{
  "message": "What's the most interesting project in your GitHub?",
  "history": [
    { "role": "user", "content": "Hi" },
    { "role": "assistant", "content": "Hey! Ask me about my projects." }
  ]
}
```
`history` excludes the system prompt (server owns that) and excludes the current `message` (sent separately). Roles: `"user" | "assistant"` only — no client-supplied tool messages.

Response: `Content-Type: text/event-stream`. Event types (SSE `event:` + JSON `data:`):

| event | data shape | meaning |
|---|---|---|
| `status` | `{"message": "checking GitHub..."}` | optional UX ping while a tool call is in flight |
| `token` | `{"text": "..."}` | a chunk of the final streamed answer |
| `done` | `{}` | stream complete, close connection |
| `error` | `{"message": "...", "recoverable": true/false}` | something failed; client should stop reading and show the message |

Example raw stream:
```
event: status
data: {"message": "checking GitHub..."}

event: token
data: {"text": "Sure, "}

event: token
data: {"text": "my favorite project is..."}

event: done
data: {}
```

### `GET /health`
Returns `200 {"status": "ok"}` with no side effects — used by the hosting platform's health checks. Does **not** call the LLM or GitHub (keep it cheap and dependency-free so it doesn't false-negative on transient upstream issues).

## 7. The Agent Loop (`agent.py`)

Core design decision: **the "decide" call is non-streaming; only the final answer streams.** Reason: tool-call delta streaming is inconsistently implemented across different OpenAI-compatible backends (some self-hosted servers only return complete tool calls in non-streamed responses). We need the complete tool-call JSON before we can execute anything anyway, so streaming that call buys nothing but adds fragile parsing. The final natural-language answer is what users actually perceive as "typing," so that's the only part worth streaming.

Pseudocode:

```python
def handle_turn(message, history):
    messages = build_messages(system_prompt, history, message)  # see §8 for budgeting
    iterations = 0

    while iterations < MAX_TOOL_ITERATIONS:
        response = llm.chat.completions.create(
            model=MODEL, messages=messages, tools=[GITHUB_TOOL_SCHEMA],
            stream=False,
        )
        choice = response.choices[0]

        if choice.finish_reason != "tool_calls":
            # Model wants to answer directly — re-issue this exact state as a
            # streaming call so the client gets tokens. (Some providers support
            # resuming from here without a second call — treat that as an
            # optimization, not a requirement; the simple/robust path is a
            # second call with stream=True and identical `messages`.)
            yield from stream_final_answer(messages)
            return

        tool_call = choice.message.tool_calls[0]   # only one tool exists
        yield sse_status(f"checking GitHub...")
        result = github_tool.execute(tool_call.arguments)   # always shaped/truncated, see §9
        messages.append(choice.message)
        messages.append(tool_result_message(tool_call.id, result))
        iterations += 1

    # Safety valve: too many tool rounds — answer with whatever we have,
    # explicitly telling the model tool access is exhausted for this turn.
    messages.append(system_note("Tool budget exhausted for this turn. Answer using only what you already have."))
    yield from stream_final_answer(messages)
```

**Failure modes to handle explicitly:**
- Model returns a malformed/unparseable tool call → catch, emit an `error` event with `recoverable: true`, and retry once by re-prompting without tools (degrade to a plain answer) rather than crashing the stream.
- GitHub call fails (network, 404, rate-limited) → return a structured error *string* as the tool result (not an exception) so the model can react conversationally ("I couldn't reach GitHub just now") instead of the request dying.
- LLM provider call fails/times out → emit `error` event, close stream. Do not retry indefinitely; one retry with backoff, then fail.

## 8. Token Budget (`budget.py`)

Given `MAX_CONTEXT_TOKENS=30000` is a **combined input+output** ceiling on a stateless service (client resends full history every turn), this is the module most likely to bite in production. Rules:

1. Effective input budget = `MAX_CONTEXT_TOKENS - RESERVED_OUTPUT_TOKENS`.
2. Token counting is approximate — a character-length heuristic (`len(text) // 4`) is sufficient; exact tokenizer parity with whatever model is plugged in isn't worth chasing given providers vary.
3. Build order when assembling `messages`: system prompt (fixed cost, log it at startup) → tool schema (fixed cost) → **most recent** history turns → current message. If the running total would exceed the effective input budget, **drop the oldest history turns first** until it fits. This is a hard truncation, not summarization — keep it simple.
4. This is a defensive backstop. The primary responsibility for trimming history length still lives client-side; document this clearly in the README so frontend implementers know not to send unbounded history.

## 9. GitHub Tool (`github_tool.py`)

One tool, `github_lookup`, exposed to the model with this schema:

```json
{
  "type": "function",
  "function": {
    "name": "github_lookup",
    "description": "Read-only lookup against a single public GitHub profile. Cannot write, cannot access private data.",
    "parameters": {
      "type": "object",
      "properties": {
        "action": {
          "type": "string",
          "enum": [
            "list_repos", "get_repo_details", "get_readme",
            "get_file_tree", "get_file_content", "get_languages",
            "get_profile", "get_pinned_repos", "get_recent_activity"
          ]
        },
        "repo": { "type": "string", "description": "Repo name, required for repo-scoped actions" },
        "path": { "type": "string", "description": "File path, required for get_file_content" }
      },
      "required": ["action"]
    }
  }
}
```

**Defense in depth:** even though `GITHUB_TOKEN` is expected to be a fine-grained PAT with no elevated scope, the tool implementation must explicitly filter `private: false` on every repo-returning call and reject any action targeting a repo not owned by `GITHUB_USERNAME`. Never trust the token's scope alone — enforce the "public only, this user only" rule in application code.

**Output shaping (every action, before it goes back to the model):**
- `list_repos` → name, description, primary language, star count only. No full payload. Cap at ~30 repos, sorted by most-recently-updated or most-starred (pick one, document the choice).
- `get_readme` → truncate to a fixed character cap (e.g. ~4000 chars) with a note appended if truncated.
- `get_file_tree` → cap depth (e.g. 3 levels) and total entry count (e.g. 200); never return binary/vendor/lockfile-heavy directories (`node_modules`, `dist`, `.git`, etc. — filter by common ignore patterns).
- `get_file_content` → hard size cap (e.g. ~6000 chars); refuse (with a clear message back to the model) for binary files.
- `get_languages`, `get_profile`, `get_pinned_repos`, `get_recent_activity` → already small; pass through with minor field pruning.

Use GitHub's authenticated REST API (5,000 req/hr with a token vs. 60/hr unauthenticated) — this is why `GITHUB_TOKEN` is required (Q6). Handle `403`/secondary-rate-limit responses by surfacing a clean error string to the model rather than propagating a raw exception.

## 10. Rate Limiting (`rate_limit.py`)

Simple in-memory sliding window keyed by client IP, `RATE_LIMIT_PER_MINUTE` requests/minute, toggleable via `RATE_LIMIT_ENABLED`. On limit exceeded, return `429` before any LLM/GitHub call is made. No external dependency — acceptable given the single-instance deployment assumption (§2). If someone later needs multi-instance scaling, this is the component to swap for Redis; note that explicitly in a code comment so it's easy to find.

## 11. CORS

Restrict to `ALLOWED_ORIGIN` from `.env` — do not default to `*` in any committed example beyond the `.env.example` placeholder. Document in the README that this must be set to the real portfolio domain before deploying publicly.

## 12. Frontend Integration Reference (Next.js + `@microsoft/fetch-event-source`)

```ts
import { fetchEventSource } from "@microsoft/fetch-event-source";

async function sendMessage(message: string, history: {role: string, content: string}[], onToken: (t: string) => void, onStatus: (s: string) => void, onDone: () => void, onError: (e: string) => void) {
  await fetchEventSource(`${process.env.NEXT_PUBLIC_AGENT_URL}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, history }),
    onmessage(ev) {
      const data = JSON.parse(ev.data);
      if (ev.event === "token") onToken(data.text);
      else if (ev.event === "status") onStatus(data.message);
      else if (ev.event === "done") onDone();
      else if (ev.event === "error") onError(data.message);
    },
    onerror(err) {
      onError(String(err));
      throw err; // stop retrying — this is a terminal error for the turn
    },
  });
}
```

Note the `throw err` in `onerror` — `fetch-event-source` retries indefinitely by default, which is wrong for a single chat turn; throwing tells it to stop.

## 13. Deployment

- Ship a single-stage `Dockerfile` (Python slim base, `uvicorn app.main:app --host 0.0.0.0 --port 8000`).
- Reference deployment target: **Oracle Cloud "Always Free"** VM — genuinely free, persistent compute, no sleep/scale-to-zero (unlike Render's free tier, which sleeps after 15 min idle, or Fly.io, which no longer offers a free tier to new accounts). Document as the recommended default in the README, but keep the app itself platform-agnostic — it's just a Docker container behind Uvicorn, so it runs anywhere.
- `GET /health` for the platform's health checks, no upstream calls (§6).

## 14. Testing Recommendations

- Unit test `budget.py`'s truncation logic with synthetic oversized histories.
- Unit test `github_tool.py`'s output shaping (truncation caps, private-repo filtering, path traversal rejection on `get_file_content`).
- Integration test the `/chat` loop against a mocked OpenAI-compatible server (both the "answers directly" path and the "calls the tool once/twice/hits MAX_TOOL_ITERATIONS" paths).
- Manual smoke test against the real configured provider + real GitHub account before first deploy.

## 15. Build Order (suggested milestones)

1. `config.py` + `.env.example` + startup validation.
2. `github_tool.py` in isolation (script it, verify shaping/caps against your real GitHub account).
3. `llm_client.py` + a bare non-streaming `/chat` that ignores tools (prove the OpenAI-compatible connection works end to end).
4. Add the tool schema + non-streaming decide loop (no SSE yet — return the final answer as plain JSON).
5. Add `budget.py` truncation.
6. Convert the final-answer path to SSE streaming; wire up `sse.py`.
7. Add `rate_limit.py` and CORS lockdown.
8. Dockerfile + deploy to Oracle Always Free; wire the Next.js client.
9. Write the fork-facing README (edit `.env` + `system_prompt.md`, nothing else).

## 16. Open Assumptions to Confirm Before/During Build

- Next.js client talks directly to this service's URL (no Firebase Cloud Function proxy in the request path).
- Repo list sort order (most-recently-updated vs. most-starred) — pick one, it's a minor call.
- `RESERVED_OUTPUT_TOKENS=1000` and truncation caps in §9 are starting points, not hard requirements — tune once real usage is observed.
