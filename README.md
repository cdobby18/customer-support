# AI Customer Support Automation

A production-ready starter blueprint for an intelligent customer support automation platform that combines conversational AI, ticket triage, routing, workflow automation, and human agent assist.

## Overview

This project is designed for an AI-first support system that can:

- accept customer requests from email, chat, WhatsApp, or web forms
- classify issue type and urgency automatically
- retrieve answers from a knowledge base using RAG
- draft customer-facing responses in brand tone
- escalate risky or unresolved issues to human agents
- track performance with SLA, CSAT, and resolution metrics

## Documentation

Full documentation lives in [docs/](docs/README.md): architecture, setup,
configuration, API reference, data model, frontend, AI agents, integrations,
testing, deployment, and operations runbooks.

## Core Features

- Ticket intake and normalization
- Intent and sentiment classification
- Smart routing and prioritization
- FAQ / policy retrieval with semantic search
- AI-generated response drafting
- Human approval and escalation logic
- Analytics and feedback loop
- CRM / support system integrations

## Stack

- Frontend: React + Vite, `lucide-react` icons
- Backend: FastAPI + Uvicorn, SQLAlchemy, Pydantic
- Auth: JWT (PyJWT), Argon2 password hashing (pwdlib)
- Database: SQLite by default, PostgreSQL optional via `DATABASE_URL` (Alembic migrations)
- Semantic search: sentence-transformers embeddings + FAISS inner-product index
- Queue: Celery + Redis, eager mode by default so no broker is needed in dev
- AI: provider-agnostic LLM gateway (mock / OpenAI / Azure OpenAI)
- Observability: OpenTelemetry tracing + structured JSON logs
- Load testing: Locust

## Run Locally

### Prerequisites

- Python 3.14+
- Node.js 18+
- Docker Desktop only if you want PostgreSQL instead of the default SQLite database

### 1. Install backend dependencies

From the project root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

PowerShell activation is optional. Using `.venv\Scripts\python.exe` avoids
interpreter mismatches.

### 2. Apply database migrations

Run this before starting the API, on a fresh checkout and after pulling any
change that adds a migration:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
```

Skipping this is the cause of `sqlite3.OperationalError: no such column:
tickets.risk_score` when creating or listing tickets.

### 3. Start the API with SQLite

SQLite is the default and requires no database setup:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Keep this terminal running. API URLs:

- Health: `http://127.0.0.1:8000/health`
- Swagger docs: `http://127.0.0.1:8000/docs`

Start without `--reload` by default. The reloader can hang during startup
without ever binding the port, in which case the API looks dead even though
the process is running. If you use `--reload` during development, restart the
process if the port never opens. Without it, startup takes about 10 seconds
because the ML imports load up front; with it you must restart after every
backend edit.

If a port is already in use, find and stop the process holding it:

```powershell
Get-NetTCPConnection -State Listen -LocalPort 8000 |
  ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

### 4. Start the frontend

Open a second terminal in the project root:

```powershell
Push-Location frontend
npm install
npm run dev
```

Open **http://localhost:5173/** in your browser.

Use `localhost`, not `127.0.0.1`. Vite binds to `::1` (IPv6 loopback) by
default, so `http://127.0.0.1:5173/` refuses to connect and appears as a blank
page. To stop the frontend terminal, press `Ctrl+C`, then run `Pop-Location`
if needed.

### 5. Sign in

Register an account from **Create account**. Accounts created in the UI start
with customer access; admin capabilities come from a role change in the
database.

### 6. Test the application

Run the backend tests from another terminal:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Expected current result: `312 passed`.

For an end-to-end API smoke test while the API is running:

```powershell
.\scripts\smoke_test.ps1
```

### Optional configuration

The AI features (AI Draft, Agent Assist suggestions, auto-respond) stay
inactive until an LLM provider is set. Use the mock provider for local work
with no API key:

```powershell
$env:LLM_PROVIDER="mock"        # or "openai" / "azure_openai"
$env:AUTO_RESPOND_ENABLED="true"
```

Set `JWT_EXPIRE_MINUTES` to lengthen the session; it defaults to `60`.
Failed logins are throttled per email and client IP: after
`AUTH_LOGIN_MAX_ATTEMPTS` (default `5`) in `AUTH_LOGIN_LOCKOUT_SECONDS`
(default `300`) the pair is refused with `429` and a `Retry-After` header.
Signing out calls `POST /auth/logout`, which revokes that access token
server-side. See `.env.example` for the full set of variables.

### Authorization model

Three roles, and every endpoint declares which one it wants:

| Role | Reach |
|------|-------|
| `customer` | Its own tickets, its own comments, knowledge search, `/auth/me` |
| `agent` | Everything `customer` can, plus every ticket in the queue, internal notes, drafts, auto-respond, agent assist |
| `admin` | Everything `agent` can, plus `/admin/*` analytics, audit logs and user management |

The boundaries that are enforced server-side, not just hidden in the UI:

- Customers see only their own tickets. `GET /tickets` ignores a supplied
  `customer_id` for a customer and scopes to the caller instead.
- Internal notes (`is_internal`) are staff-only. They are filtered out of
  `GET /tickets/{id}/comments` for customers, and a customer cannot create one
  — a submitted `is_internal: true` is stored as `false`.
- Customers never receive internal triage output: `risk_score`, `risk_level`,
  `escalation_summary`, `escalation_route` and `intake_metadata` are `null`
  in their ticket responses, and guardrail hits keep the rule and severity but
  drop `matched`, which is the literal PII the rule caught.
- Customers can only open tickets on the `web` channel. The channel selects the
  SLA multiplier, so letting a caller choose it would let them choose their own
  deadline. Channels are otherwise supplied by the integration that received
  the message.
- An inbound channel message only joins an existing ticket thread when the
  sender matches that ticket's customer. All channels share one secret, so a
  mismatch opens a new ticket and is audited as
  `channel.thread_customer_mismatch` rather than posting into the other
  customer's conversation.
- An admin cannot deactivate another admin, matching the existing rule that an
  admin cannot delete one.

## Run with Docker

One image serves both roles. `requirements.txt` pulls in torch via
sentence-transformers, so the runtime layer is multiple gigabytes; building two
images would either duplicate that layer or leave the worker without its
dependencies. The API is the default command and the worker overrides it.

```powershell
docker build -t support-api .
```

**Migrations are a separate step, not part of startup.** `init_db()` uses
`create_all()`, which creates missing tables but never alters an existing one, so
an existing database needs Alembic. Running it inside the entrypoint would also
race when more than one replica starts at once.

```powershell
docker run --rm -e DATABASE_URL=postgresql+psycopg://user:pass@host:5432/db support-api alembic upgrade head
```

Run the API:

```powershell
docker run --rm -p 8000:8000 `
  -e DATABASE_URL=postgresql+psycopg://user:pass@host:5432/db `
  -e JWT_SECRET=your-long-random-secret `
  -e CHANNEL_WEBHOOK_SECRET=your-long-random-webhook-secret `
  -e APP_ENV=production `
  -e LLM_PROVIDER=openai `
  -e OPENAI_API_KEY=sk-... `
  -e TRUSTED_HOSTS=support.example.com `
  support-api
```

Run the worker from the same image:

```powershell
docker run --rm -e DATABASE_URL=... -e REDIS_URL=redis://redis:6379/0 `
  -e CELERY_TASK_ALWAYS_EAGER=0 `
  support-api celery -A app.core.workers worker --loglevel=info --pool=solo
```

Three details that are deliberate rather than incidental:

- **`--pool=solo`.** The worker imports the same modules as the API, so Celery's
  default prefork pool would copy a multi-gigabyte torch process per child.
- **Non-root.** The container runs as uid 10001; it holds the database
  credentials and the webhook secret, so an escape should not land on root. One
  consequence: Docker creates named volumes as root, so a non-root container
  cannot write to a fresh one — `docker run -v ...` needs the volume chowned
  first, or an entrypoint that fixes ownership.
- **`CELERY_TASK_ALWAYS_EAGER` must be `0`.** It defaults to `1`, which runs tasks
  inline in the API process and makes the worker a no-op.

The image is built and smoke-tested in CI (`docker` job: build, migrations,
`/health`, unauthenticated 401, worker task registration, non-root user), which
is what verifies it — not a local `docker build`.

## Run against real PostgreSQL and a real worker

SQLite is a development convenience and it lies in three ways that matter:
it does not enforce foreign keys, its `TIMESTAMP` has no timezone, and an
eager Celery task "running" proves nothing about the queue. The `postgres` CI
job exists for exactly this, and runs the whole suite plus a full task
round-trip against real services:

- `services:` PostgreSQL 16 and Redis 7, both health-gated
- the 300-test suite against PostgreSQL
- `alembic upgrade head` against an empty PostgreSQL database, asserting the
  tables it creates
- the API with `CELERY_TASK_ALWAYS_EAGER=0` and a real worker on a real broker
- a driver that registers a user, promotes it to admin, creates a ticket, and
  waits for the outbound webhook — then checks the task's return value came
  back out of the Redis result backend

To run the same thing by hand, point the driver at services you have running:

```powershell
$env:DATABASE_URL="postgresql+psycopg://user:pass@localhost:5432/support"
$env:REDIS_URL="redis://localhost:6379/0"
$env:CELERY_TASK_ALWAYS_EAGER="0"
$env:NOTIFY_WEBHOOK_URL="http://127.0.0.1:8099/hook"

.\.venv\Scripts\python.exe scripts\notify_receiver.py 8099
.\.venv\Scripts\python.exe -m celery -A app.core.workers worker --loglevel=debug --pool=solo
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
.\.venv\Scripts\python.exe scripts\e2e_postgres_worker.py --worker-log worker.log
```

`--loglevel=debug` is not decoration: Celery only logs its per-task "received"
line at DEBUG, and the driver asserts on that line to prove the task went
through the broker rather than being executed inline by the API. Running the
worker at INFO makes the check unable to fail.

The whole suite can also be pointed at PostgreSQL by setting `TEST_DATABASE_URL`;
unset, it uses in-memory SQLite as before.

## Success Metrics

- reduced first response time
- improved CSAT
- lower manual workload
- higher resolution rate for common issues
- better ticket routing accuracy
- lower escalation failure rate
