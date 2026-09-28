# Plan 07 — Rate Limiting & CORS

## Goal
Add in-memory per-IP rate limiting and lock down CORS to the configured `ALLOWED_ORIGIN`. Both are network-boundary concerns applied before any LLM or GitHub call is made.

## Scope
- `app/rate_limit.py` — sliding-window rate limiter
- `app/main.py` — wire rate limiting middleware and CORS

## Depends On
- Plan 01 (`Settings` — `rate_limit_enabled`, `rate_limit_per_minute`, `allowed_origin`)
- Plan 03 (`app/main.py` — app instance)

---

## Files to Create / Modify

| File | Action | Purpose |
|---|---|---|
| `app/rate_limit.py` | Create | In-memory sliding-window limiter |
| `app/main.py` | Modify | Add CORS middleware, rate-limit check on `/chat` |

---

## `app/rate_limit.py` — Design

### Algorithm: sliding window per IP

Maintain an in-memory dict keyed by client IP. Each value is a list of request timestamps (as floats from `time.monotonic()`).

On each incoming request for a given IP:
1. Drop all timestamps older than 60 seconds from the list.
2. If the list length is already at `rate_limit_per_minute`, the limit is exceeded → return `False`.
3. Otherwise, append the current timestamp and return `True`.

This is a pure in-memory structure. It's explicitly not safe for multi-process/multi-instance deployments. Include a code comment at the top of the file: _"Multi-instance note: swap this dict for a Redis-backed store (e.g. with a SCAN-based sliding window) if you scale beyond a single process."_

### `RateLimiter` class

**Constructor:** accepts `per_minute: int`. Initializes the internal `dict[str, list[float]]` storage.

**`is_allowed(ip: str) -> bool`:** implements the sliding-window logic above. Thread-safe enough for single-process async use (the GIL protects dict operations in CPython; no lock needed for single-process Uvicorn with a single worker).

**`enabled` flag:** the class is always instantiated. Whether it acts is controlled by a boolean set at construction time (from `settings.rate_limit_enabled`). If disabled, `is_allowed()` always returns `True` — no conditional logic scattered across call sites.

---

## `app/main.py` — Changes

### CORS middleware
Add `fastapi.middleware.cors.CORSMiddleware` with:
- `allow_origins=[settings.allowed_origin]` — single origin from config, not `["*"]`.
- `allow_methods=["POST", "GET", "OPTIONS"]`.
- `allow_headers=["Content-Type"]`.
- `allow_credentials=False`.

The middleware is added in the lifespan setup block so `settings.allowed_origin` is already validated before it's used.

### Rate limit check on `/chat`
At the very top of the `/chat` route handler, before message assembly:
1. Extract the client IP from `request.client.host`. If it's `None` (e.g. a Unix socket or test client), treat it as `"unknown"`.
2. Call `app.state.rate_limiter.is_allowed(ip)`.
3. If not allowed, return `429` immediately with JSON body `{"error": "Too many requests. Please wait before sending another message."}`. Do not start the SSE stream.

### Lifespan addition
Instantiate `RateLimiter(per_minute=settings.rate_limit_per_minute, enabled=settings.rate_limit_enabled)` and store it on `app.state.rate_limiter`.

---

## Guardrails

| Risk | Mitigation |
|---|---|
| CORS wildcard accidentally committed | `ALLOWED_ORIGIN` is `Field(...)` (required) — server won't start without it; `.env.example` has a comment warning against `*` |
| Rate limit bypassed by IP spoofing | This service is intended to run on a single VM, not behind an untrusted proxy; if deployed behind a trusted reverse-proxy, document that `X-Forwarded-For` parsing should be enabled via Uvicorn's `--proxy-headers` flag |
| Rate limiter state lost on restart | Acceptable for in-memory single-process design; state is intentionally ephemeral |
| Memory growth from many unique IPs | The timestamp list per IP grows only up to `per_minute` entries and prunes itself each request; dead IPs accumulate at most one empty list entry — negligible |
| Rate limit applied to `/health` | Do **not** rate-limit `/health` — it must remain cheap and always available for the platform's health checks |

---

## Verification Checklist

- [ ] `ALLOWED_ORIGIN=https://yourportfolio.com` → a cross-origin request from a different origin is rejected by the browser (CORS failure visible in dev tools).
- [ ] A same-origin (or matching-origin) request succeeds.
- [ ] Sending 21 requests in under 60 seconds (with `RATE_LIMIT_PER_MINUTE=20`) returns `429` on the 21st.
- [ ] Setting `RATE_LIMIT_ENABLED=false` in `.env` allows unlimited requests regardless of rate.
- [ ] `GET /health` is never blocked by the rate limiter.
- [ ] The `429` response body is `{"error": "Too many requests..."}` (not an unhandled exception).

## Definition of Done
CORS is locked to `ALLOWED_ORIGIN`. Rate limiting blocks excess requests before any upstream call is made. `/health` is unaffected. The limiter is disabled with a single env var. A clear code comment marks the multi-instance swap point.
