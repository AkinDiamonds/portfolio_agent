# Plan 09 — Tests & README

## Goal
Write the tests specified in the spec and produce the fork-facing README — the only documentation a person cloning this template needs to read.

## Scope
- `tests/` directory with unit and integration tests
- `README.md` (replace the stub)

## Depends On
- All previous plans (tests exercise the real modules)

---

## Files to Create / Modify

| File | Action | Purpose |
|---|---|---|
| `tests/__init__.py` | Create | Empty — marks tests as a package |
| `tests/test_budget.py` | Create | Unit tests for `budget.py` |
| `tests/test_github_tool.py` | Create | Unit tests for `github_tool.py` output shaping |
| `tests/test_chat_loop.py` | Create | Integration tests for the `/chat` loop |
| `README.md` | Modify | Full fork-facing documentation |

Add `pytest` and `pytest-asyncio` (for async route tests) to `requirements.txt` under a `[dev]` comment block — or a separate `requirements-dev.txt`. Do not make test dependencies required for the production image.

---

## `tests/test_budget.py` — Unit Tests

All tests are pure functions — no network, no env vars.

**Test cases to cover:**

1. **History fits entirely** — a history whose total tokens are well under the budget is returned unchanged.
2. **History trimmed by one turn** — a history where the last turn alone would exceed the budget; the oldest turn is dropped; the newest is kept.
3. **History trimmed to empty** — a history so large that no turns fit; the result contains only the system message and the current message.
4. **System prompt alone exceeds budget** — function does not raise; returns `[system_msg, current_msg]` and the test asserts a warning was logged.
5. **Message ordering preserved** — after trimming, the remaining history items are in chronological order (oldest first), not reversed.
6. **Current message always included** — even when all history is dropped, the current user message is the last item in the result.
7. **`estimate_tokens("")` returns at least 1** — never zero.

---

## `tests/test_github_tool.py` — Unit Tests

Use `unittest.mock.patch` to mock `httpx.Client.get` (and `.post` for GraphQL). No real HTTP calls.

**Test cases to cover:**

1. **`list_repos` caps at `REPO_LIST_CAP`** — mock returns 50 repos; assert result contains exactly 30.
2. **`list_repos` filters private repos** — mock response includes repos with `private: true`; assert they are absent from the result.
3. **`get_readme` truncates at `README_CHAR_CAP`** — mock returns a 10 000-char README; assert result length ≤ 4 000 chars and ends with the truncation note.
4. **`get_file_content` refuses binary extensions** — call with `path="image.png"`; assert the error string is returned without making any HTTP call.
5. **`get_file_content` truncates at `FILE_CONTENT_CHAR_CAP`** — mock returns a 20 000-char file; assert result length ≤ 6 000 chars.
6. **`get_file_tree` ignores `node_modules`** — mock returns a tree with `node_modules/index.js`; assert it's absent from the result.
7. **`get_file_tree` respects `FILE_TREE_MAX_DEPTH`** — mock returns a deeply nested path; assert paths exceeding depth 3 are excluded.
8. **Foreign repo ownership rejected** — call `execute({"action": "list_repos", "repo": "otheruser/somerepo"})`; assert error string, assert no HTTP call was made.
9. **Bad action name returns error string** — call `execute({"action": "delete_everything"})`; assert result is a string starting with `[Tool error]`, no exception raised.
10. **GitHub 404 returns error string** — mock returns `404`; assert `execute()` returns a descriptive string, not an exception.
11. **GitHub 429 rate-limit returns error string** — mock returns `429`; assert `execute()` returns a descriptive string.
12. **Missing required `repo` arg returns error string** — call `get_readme` without `repo`; assert validation error string.

---

## `tests/test_chat_loop.py` — Integration Tests

Use a mocked OpenAI-compatible server (implement as a simple `httpx_mock` or monkeypatching `openai.OpenAI.chat.completions.create`). No real LLM calls, no real GitHub calls.

**Test cases to cover:**

1. **Direct answer path** — mock returns `finish_reason="stop"` with a content string immediately; assert the SSE stream yields `token` events followed by `done`, no `status` event.
2. **Single tool call path** — mock returns `finish_reason="tool_calls"` on the first call, then `finish_reason="stop"` on the second; mock `GithubTool.execute()` to return a fixed string; assert `status` event appears before `token` events.
3. **Two tool calls** — same as above but two rounds before the model answers; assert two `status` events.
4. **`MAX_TOOL_ITERATIONS` exhausted** — set `max_tool_iterations=1`; mock always returns `finish_reason="tool_calls"`; assert the final stream contains a `token` event (the exhaustion fallback answer) and a `done` event — no crash, no infinite loop.
5. **LLM error → `error` event** — mock `create()` raises `openai.APIError`; assert the stream yields an `error` event with `recoverable=False` and then terminates.
6. **Malformed tool call → degraded plain answer** — mock returns `finish_reason="tool_calls"` but with invalid JSON in the tool call; assert the stream produces a `token`+`done` sequence (degraded answer) rather than an `error` event.
7. **Rate limit → 429 JSON** — set `RATE_LIMIT_PER_MINUTE=1`; send two requests from the same IP; assert the second returns HTTP 429 with a JSON body, not an SSE stream.
8. **Empty message → 422** — post `{"message": "", "history": []}`; assert HTTP 422.
9. **Invalid history role → 422** — post history containing `role: "system"`; assert HTTP 422.

---

## `README.md` — Structure

The README is the only document a forker reads. Keep it short and direct.

### Sections (in order):

1. **What this is** — one paragraph: a self-hostable Python backend powering a chat agent for a developer portfolio. Links to the spec for deep context.
2. **Prerequisites** — Python 3.12+, Docker (for deployment), a GitHub fine-grained PAT, an OpenAI-compatible LLM provider.
3. **Quickstart (local)** — three steps: clone, copy `.env.example` → `.env` and fill it, run `uvicorn app.main:app --reload`. No other steps.
4. **The two files you edit** — a dedicated callout: the only files a forker is expected to touch are `.env` and `system_prompt.md`. Nothing else.
5. **Environment variable reference** — a table: variable name, description, required/optional, example value. One row per variable. No duplication — this is the canonical reference.
6. **Client history note** — a short paragraph: this service is stateless; the client is responsible for sending conversation history each turn and should trim it to a reasonable length (≤ 20 turns recommended). The server has a backstop but it's not a substitute for client-side discipline.
7. **Supported LLM providers** — a short list of tested/known-compatible providers (OpenRouter, Groq, Together, Fireworks, DeepSeek, vLLM, Ollama, LM Studio). Note: any OpenAI-compatible endpoint works.
8. **Deployment (Oracle Always Free)** — summary from Plan 08. Link to Oracle docs. `--workers 1` constraint called out explicitly.
9. **Frontend integration** — the `fetchEventSource` usage pattern from the spec (§12), described in prose: POST to `/chat`, consume SSE events by type, throw in `onerror` to stop retrying.
10. **Forking checklist** — a short numbered list: (1) copy `.env`, (2) edit `system_prompt.md`, (3) build Docker image, (4) deploy with `--env-file`, (5) set CORS origin to real domain.

---

## Guardrails

| Risk | Mitigation |
|---|---|
| Tests hit real network | All HTTP calls mocked; tests are marked to fail fast if a real network call is detected (use `httpx_mock` or `responses` library) |
| Test env vars not set | Tests that need `Settings` use a hardcoded fixture dict, not a real `.env` |
| README gives a `*` CORS example | Explicitly say in the CORS variable row: "Never use `*` in production" |
| Forkers miss the `--workers 1` constraint | Called out in the deployment section and in a code comment in `rate_limit.py` |
| Test file imports break if module structure changes | Tests import from the `app` package directly; a broken import is a test failure, not a silent pass |

---

## Verification Checklist

- [ ] `pytest tests/test_budget.py` — all cases pass with no network calls.
- [ ] `pytest tests/test_github_tool.py` — all cases pass with no network calls.
- [ ] `pytest tests/test_chat_loop.py` — all cases pass; mocks are in place.
- [ ] `pytest` (all tests) exits with code 0.
- [ ] README renders correctly on GitHub (check heading hierarchy, table formatting).
- [ ] README contains no placeholder text (`[YOUR NAME]`, `yourportfolio.com`, etc.) that a forker could miss.

## Definition of Done
All tests green. README is the only document a forker needs to deploy a working instance. No code or tests reference real credentials or real network endpoints.
