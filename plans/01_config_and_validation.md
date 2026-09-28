# Plan 01 — Config & Startup Validation

## Goal
Bootstrap the project skeleton and validated configuration layer. The service must refuse to start if any required value is missing or malformed — no silent broken states.

## Scope
- Project skeleton (folder structure, `requirements.txt`)
- `app/config.py` — typed settings via `pydantic-settings`
- `.env.example` — canonical reference for all env vars
- `system_prompt.md` — starter template for forkers

## Out of Scope
Everything else. No routes, no LLM calls, no GitHub calls.

---

## Files to Create

| File | Purpose |
|---|---|
| `app/__init__.py` | Empty — marks `app/` as a package |
| `app/config.py` | Settings class + startup loaders |
| `.env.example` | Documents every env var with inline comments |
| `system_prompt.md` | Starter system prompt template |
| `requirements.txt` | Pinned dependency list |

---

## Dependencies (`requirements.txt`)

Only what's needed for the full build — nothing extra:

- `fastapi` + `uvicorn[standard]` — web framework
- `pydantic` + `pydantic-settings` — validation and config
- `openai` — LLM client (OpenAI-compatible)
- `httpx` — GitHub REST calls
- `python-dotenv` — `.env` file loading

No LiteLLM. No PyGithub. No abstraction libraries.

---

## `app/config.py` — Design Decisions

### Settings class
Extend `pydantic-settings` `BaseSettings` with `SettingsConfigDict(env_file=".env", extra="ignore")`.

### Required fields (no default → fast-fail if absent)
- `llm_base_url` — OpenAI-compatible provider URL
- `llm_api_key` — provider API key
- `llm_model` — model identifier
- `github_username` — portfolio owner's GitHub handle
- `github_token` — fine-grained PAT (public read)
- `allowed_origin` — CORS origin; required so deployers can't forget it

### Optional fields with safe defaults
- `max_context_tokens` — default 30 000, min 1 000
- `reserved_output_tokens` — default 1 000, min 100
- `max_tool_iterations` — default 4, range 1–10
- `rate_limit_enabled` — default `true`
- `rate_limit_per_minute` — default 20, min 1

### Validators
- `llm_base_url`: must start with `http`; trailing slash stripped.

### Computed property
- `effective_input_budget` → `max_context_tokens − reserved_output_tokens`. Used by `budget.py`. Defined here so it's always consistent with the two source fields.

### Startup loaders (plain functions, not methods)
- `load_settings()` — instantiates `Settings`; raises `ValidationError` on failure. Called once at app startup.
- `load_system_prompt(path)` — reads `system_prompt.md`; raises `FileNotFoundError` if missing, `ValueError` if empty. Logs char count at INFO level on success.

---

## `.env.example` — Variables to Document

Group comments by concern:

1. **LLM provider** — `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`
2. **Context budget** — `MAX_CONTEXT_TOKENS`, `RESERVED_OUTPUT_TOKENS`
3. **GitHub** — `GITHUB_USERNAME`, `GITHUB_TOKEN` (with note: fine-grained PAT, no elevated scope needed)
4. **Agent behavior** — `MAX_TOOL_ITERATIONS`
5. **Networking** — `ALLOWED_ORIGIN` (with warning: set to real domain, never `*`), `RATE_LIMIT_ENABLED`, `RATE_LIMIT_PER_MINUTE`

---

## `system_prompt.md`

Minimal starter text instructing the model who it is, what it has access to (`github_lookup` tool), and the expected tone. Forkers replace this with their own content — it is the primary customization surface alongside `.env`.

---

## Guardrails

| Risk | Mitigation |
|---|---|
| Missing required env var | `Field(...)` with no default → Pydantic raises `ValidationError` with field-level message before the server binds a port |
| Malformed `LLM_BASE_URL` | Custom `field_validator` rejects non-HTTP values |
| Missing `system_prompt.md` | `load_system_prompt` raises `FileNotFoundError` immediately |
| Empty `system_prompt.md` | `load_system_prompt` raises `ValueError` |
| Unknown env vars polluting settings | `extra="ignore"` on the settings model |
| `effective_input_budget` going negative | Min constraints on both token fields ensure budget is always positive |

---

## Verification Checklist

- [ ] With a valid `.env` and `system_prompt.md`, `load_settings()` and `load_system_prompt()` both succeed and log expected output.
- [ ] Removing any required field from `.env` produces a `ValidationError` naming the missing field — server does not start.
- [ ] Setting `LLM_BASE_URL` to a non-HTTP string produces a validator error.
- [ ] Deleting `system_prompt.md` produces `FileNotFoundError`.
- [ ] `effective_input_budget` equals `MAX_CONTEXT_TOKENS − RESERVED_OUTPUT_TOKENS`.

## Definition of Done
Config layer is the only thing that exists. No routes, no LLM calls, no HTTP clients. This plan is done when the verification checklist is fully green.
