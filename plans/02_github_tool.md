# Plan 02 — GitHub Tool

## Goal
Implement `app/github_tool.py` — the one and only agent tool. All GitHub REST calls live here, with strict output shaping, hard size caps, and guaranteed string returns (errors are returned as informative strings, never raised as exceptions).

## Scope
- `app/github_tool.py` — tool class, tool schema constant, Pydantic input model

## Depends On
- Plan 01 (`Settings` — needs `github_username`, `github_token`)

---

## Files to Create

| File | Purpose |
|---|---|
| `app/github_tool.py` | Tool class, input validation model, tool schema constant |

---

## Core Design Principles

1. **Never raise into the agent loop.** The public `execute()` method always returns a `str`. On any failure — network error, 404, bad args, rate-limit — return a descriptive error string so the model can react conversationally ("I couldn't reach GitHub just now") rather than crashing the stream.
2. **Explicit action allow-list.** Actions are a `Literal` type enforced by Pydantic. Anything not on the list is rejected before any HTTP call is made.
3. **Defense in depth on access control.** Even though the token is fine-grained and low-scope, code must explicitly filter `private: false` on every repo-returning call and reject any action targeting a repo not owned by `GITHUB_USERNAME`. Never trust the token's scope alone.
4. **Hard output caps.** Every action shapes and truncates before returning. Caps are named constants at the top of the file — easy to tune without touching logic.

---

## Named Constants (top of file, all tunable)

| Constant | Value | Applies To |
|---|---|---|
| `REPO_LIST_CAP` | 30 | `list_repos` — max repos returned |
| `README_CHAR_CAP` | 4 000 | `get_readme` — max characters |
| `FILE_TREE_MAX_DEPTH` | 3 | `get_file_tree` — directory recursion depth |
| `FILE_TREE_MAX_ENTRIES` | 200 | `get_file_tree` — total entry count |
| `FILE_CONTENT_CHAR_CAP` | 6 000 | `get_file_content` — max characters |
| `IGNORED_TREE_NAMES` | `node_modules`, `dist`, `build`, `.git`, `__pycache__`, `.venv`, `venv`, `vendor`, `.next`, `coverage`, `.cache` | `get_file_tree` — directories to skip |
| `BINARY_EXTENSIONS` | `.png`, `.jpg`, `.jpeg`, `.gif`, `.svg`, `.ico`, `.webp`, `.pdf`, `.zip`, `.tar`, `.gz`, `.wasm`, `.ttf`, `.woff`, `.woff2`, `.eot`, `.mp4`, `.mp3`, `.pyc` | `get_file_content` — refuse binary files |

---

## Pydantic Input Model (`GithubLookupArgs`)

Validates the model's raw tool-call arguments before any HTTP call.

**Fields:**
- `action` — `Literal` of the 9 allowed action names; Pydantic rejects anything else automatically
- `repo` — optional string; required for repo-scoped actions (validated in `model_validator`)
- `path` — optional string; required for `get_file_content` (validated in `model_validator`)

**`model_validator` (after):** If `action` is in the repo-scoped set and `repo` is absent, raise `ValueError`. If `action` is `get_file_content` and `path` is absent, raise `ValueError`. These become the error string returned to the model — they never propagate further.

---

## `GithubTool` Class

### Constructor
Accepts `username: str` and `token: str`. Stores the username in lowercase. Creates a shared `httpx.Client` with the GitHub auth header, JSON accept header, API version header, and a 10-second timeout.

### `execute(raw_args)` — public entry point
1. Parse `raw_args` (string or dict) into `GithubLookupArgs` via `model_validate`. On parse/validation failure → return error string.
2. If `args.repo` is set, run `_is_own_repo()` check → return error string if it fails.
3. Dispatch to the matching private handler.
4. Wrap the handler call in a broad `try/except` that logs the exception and returns a generic error string — last-resort safety net.

### `_is_own_repo(repo)` — internal guard
If `repo` contains a `/`, extract the owner segment and compare (case-insensitive) to `_username`. Bare repo names (no `/`) are assumed to belong to this user. Returns `bool`.

### `_get(url, params)` — internal HTTP helper
Performs a GET via the shared client. Returns parsed JSON on success. Returns `None` on 404, 403, 429, or any `httpx.HTTPError` — never raises. Logs rate-limit responses at WARNING level.

### `_format_error(action, detail)` — internal helper
Returns a human-readable string like `"GitHub 'list_repos' is unavailable right now: <detail>"`. Used consistently across all action handlers so the model gets uniform messaging.

---

## Action Handlers (all private, all return `str`)

### `list_repos`
- Calls `GET /users/{username}/repos` with `type=public`, `sort=updated`, `per_page=100`.
- Filters out any item where `private` is true.
- Caps at `REPO_LIST_CAP`.
- Shapes each item to: `name`, `description`, `language`, `stars`, `updated_at`. Drops all other fields.
- Sort order: most recently updated (documented choice — consistent with sort param).

### `get_repo_details`
- Calls `GET /repos/{username}/{repo}`.
- Returns error string if `private` is true.
- Shapes to: `name`, `description`, `language`, `stars`, `forks`, `open_issues`, `topics`, `homepage`, `created_at`, `updated_at`.

### `get_readme`
- Calls `GET /repos/{username}/{repo}/readme`.
- Base64-decodes the `content` field.
- Truncates to `README_CHAR_CAP` characters. Appends a truncation note if cut.

### `get_file_tree`
- Calls `GET /repos/{username}/{repo}/git/trees/HEAD` with `recursive=1`.
- Extracts only `blob` entries (files, not trees).
- Filters via `_filter_tree()`: skip any path whose depth exceeds `FILE_TREE_MAX_DEPTH + 1`, skip any path segment matching `IGNORED_TREE_NAMES`.
- Caps total entries at `FILE_TREE_MAX_ENTRIES`; appends omission note if cut.
- Returns as a newline-separated string of paths.

### `get_file_content`
- Checks `path` extension against `BINARY_EXTENSIONS` before making any API call — returns error string immediately for binary files.
- Calls `GET /repos/{username}/{repo}/contents/{path}`.
- If the response is a list, the path is a directory — returns error string.
- Base64-decodes content; truncates to `FILE_CONTENT_CHAR_CAP`; appends truncation note if cut.

### `get_languages`
- Calls `GET /repos/{username}/{repo}/languages`.
- Returns the response as-is (already a small, clean `{"Language": bytes}` object).

### `get_profile`
- Calls `GET /users/{username}`.
- Shapes to: `login`, `name`, `bio`, `company`, `location`, `public_repos`, `followers`, `following`.

### `get_pinned_repos`
- Uses the GitHub GraphQL endpoint (`POST /graphql`) since REST doesn't expose pinned repos.
- Query requests up to 6 `pinnedItems` of type `REPOSITORY`.
- Shapes each to: `name`, `description`, `language`, `stars`.
- Wrapped in `try/except` — GraphQL failures return `_format_error` string.

### `get_recent_activity`
- Calls `GET /users/{username}/events/public` with `per_page=10`.
- Shapes each item to: `type`, `repo`, `created_at`. Returns first 10.

---

## Tool Schema Constant (`GITHUB_TOOL_SCHEMA`)

Defined as a plain dict constant in this file. Used by `agent.py`. Describes the `github_lookup` function with the 9 allowed `action` enum values, optional `repo` string, and optional `path` string. `action` is the only required parameter.

---

## Guardrails Summary

| Risk | Mitigation |
|---|---|
| Model sends malformed JSON args | `json.loads` + `model_validate` — returns error string on failure |
| Model requests a private repo | `private: false` filter + explicit error return; never relies on token scope alone |
| Model requests another user's repo | `_is_own_repo()` blocks before any HTTP call |
| GitHub 403 / 429 rate-limit | `_get()` returns `None`; handler returns `_format_error()` string |
| GitHub network timeout | `httpx.HTTPError` caught in `_get()` → returns `None` → error string |
| Binary file requested | Extension check happens before any API call |
| Response too large for context | Hard char/entry caps with truncation notes |
| Unexpected exception in handler | Outer `try/except` in `execute()` logs and returns generic error string |

---

## Verification Checklist

Run as a standalone script against the real configured GitHub account (no server needed):

- [ ] `get_profile` returns the expected user fields.
- [ ] `list_repos` returns ≤ 30 repos, sorted by `updated_at` descending, public only.
- [ ] `get_pinned_repos` returns up to 6 pinned repos.
- [ ] `get_readme` for a large repo returns ≤ 4 000 chars with a truncation note.
- [ ] `get_file_content` for a binary file returns an error string (no API call made).
- [ ] `get_file_content` for a non-existent path returns a `_format_error` string.
- [ ] An unknown action name returns a Pydantic validation error string.
- [ ] A repo-scoped action without `repo` returns a validation error string.
- [ ] A repo name with a foreign owner prefix returns an ownership error string.

## Definition of Done
The tool can be exercised in isolation as a standalone script. All 9 actions succeed or return clean error strings. No Python exceptions escape `execute()`.
