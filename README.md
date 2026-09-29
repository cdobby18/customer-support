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

Expected current result: `202 passed`.

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

## Success Metrics

- reduced first response time
- improved CSAT
- lower manual workload
- higher resolution rate for common issues
- better ticket routing accuracy
- lower escalation failure rate
