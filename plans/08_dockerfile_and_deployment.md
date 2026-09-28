# Plan 08 — Dockerfile & Deployment

## Goal
Package the service as a single-stage Docker image and document the deployment process for Oracle Cloud Always Free VM — the recommended reference target. The app itself remains platform-agnostic.

## Scope
- `Dockerfile`
- `.dockerignore`
- Deployment documentation section in `README.md`

## Depends On
- All previous plans (the full app must be complete before containerizing)

---

## Files to Create / Modify

| File | Action | Purpose |
|---|---|---|
| `Dockerfile` | Create | Single-stage build, Python slim base |
| `.dockerignore` | Create | Keep image lean |
| `README.md` | Modify | Deployment section |

---

## `Dockerfile` — Design

### Base image
`python:3.12-slim` — minimal footprint, no dev tools.

### Build steps (in order)
1. Set working directory to `/app`.
2. Copy `requirements.txt` first (before application code) — leverages Docker layer caching so dependencies are not re-installed on every code change.
3. Run `pip install --no-cache-dir -r requirements.txt`.
4. Copy the rest of the application files.
5. Create a non-root user and switch to it — never run the server as root.
6. Expose port `8000`.
7. Set the default command: `uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1`.

### Why `--workers 1`
Single-worker is mandatory for this design: the in-memory rate limiter in Plan 07 is not safe across multiple processes. The comment in `rate_limit.py` (from Plan 07) already flags the Redis swap point for anyone who later needs multi-worker. Do not use `--workers` > 1 here and document this constraint clearly.

### No `CMD` / `ENTRYPOINT` split complexity
Use a simple `CMD` list. No entrypoint script needed — environment variables are the only configuration surface.

---

## `.dockerignore`

Exclude from the build context:
- `.git/`
- `.env` (secrets must never be baked into the image — injected at runtime via env vars or a secrets manager)
- `__pycache__/`, `*.pyc`, `*.pyo`
- `*.md` files (not needed at runtime, but `system_prompt.md` IS needed — do not exclude `*.md` globally; instead exclude specific files like `README.md`, `plans/`)
- `plans/`
- `.venv/`, `venv/`
- `tests/`

**Important:** `system_prompt.md` must be included in the image (or mounted at runtime). The simplest approach is to include it in the image (it's not a secret). Document in the README that forkers can also mount it via a Docker volume or bind mount if they want to update it without rebuilding.

---

## Oracle Cloud Always Free — Deployment Notes (for `README.md`)

Document the following steps at a high level (not a step-by-step tutorial — link to Oracle docs):

1. **Provision**: Create an `Ampere A1` (ARM) Compute instance — Always Free tier. Choose Ubuntu 22.04.
2. **Install Docker**: Standard Ubuntu Docker install (`apt`).
3. **Clone the repo** onto the VM.
4. **Create `.env`** from `.env.example`. Fill in all required values. Store it outside the repo directory (e.g. `~/portfolio-agent.env`) and reference it with `--env-file` in the Docker run command.
5. **Build the image**: `docker build -t portfolio-agent .`
6. **Run the container**: `docker run -d --restart unless-stopped --env-file ~/portfolio-agent.env -p 8000:8000 portfolio-agent`
7. **Expose port 8000** in Oracle's Security List (or use Nginx as a reverse proxy on port 443 with a TLS cert from Let's Encrypt — recommended for production).
8. **Health check**: the platform (or a simple systemd timer) can poll `GET /health` — it always returns `{"status": "ok"}` with no upstream calls.

### Why Oracle Always Free?
- Genuinely free and persistent (no sleep/scale-to-zero).
- Render's free tier sleeps after 15 min idle. Fly.io no longer offers a free tier to new accounts.
- The `Ampere A1` shape gives 4 vCPUs + 24 GB RAM on the free tier — vastly more than this service needs.
- The app is just a Docker container behind Uvicorn — it runs identically on any other platform (Railway, Fly.io paid, self-hosted VPS).

---

## Guardrails

| Risk | Mitigation |
|---|---|
| `.env` secrets baked into image | `.dockerignore` excludes `.env`; only injected at runtime via `--env-file` or equivalent |
| Running as root in container | Dockerfile creates and switches to a non-root user |
| Multi-worker breaking rate limiter | `--workers 1` enforced in `CMD`; constraint documented in README and in `rate_limit.py` comment |
| `system_prompt.md` missing from image | Not in `.dockerignore`; included by default; README documents volume-mount alternative |
| ARM vs AMD64 image mismatch | `python:3.12-slim` is a multi-arch image; both architectures are covered automatically |

---

## Verification Checklist

- [ ] `docker build -t portfolio-agent .` completes without error.
- [ ] `docker run --env-file .env -p 8000:8000 portfolio-agent` starts the server; logs show `"Server ready"`.
- [ ] `curl http://localhost:8000/health` returns `{"status": "ok"}`.
- [ ] `curl -X POST http://localhost:8000/chat -H "Content-Type: application/json" -d '{"message":"hi","history":[]}'` returns an SSE stream.
- [ ] Removing a required env var from the env file causes the container to exit with a `ValidationError` log — not a silent hang.
- [ ] The running image process is not `root` (`docker exec <id> whoami` returns the non-root username).
- [ ] `.env` is not present inside the image (`docker run ... sh -c "ls -la /app"` does not show `.env`).

## Definition of Done
The service runs in Docker with environment-variable-only configuration. No secrets are in the image. Health check works. The README deployment section is clear enough for a developer unfamiliar with Oracle Cloud to follow.
