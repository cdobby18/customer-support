# AI Customer Support Automation

A customer support platform that triages incoming tickets, drafts replies from a
knowledge base, and escalates anything risky to a human agent.

Every AI feature degrades gracefully. With no `LLM_PROVIDER` set the system runs
on deterministic rules and stays fully functional, so nothing here depends on a
model being available.

## Features

- Ticket intake from web, email, chat, and WhatsApp
- Intent, priority, and sentiment classification with smart routing
- Retrieval-augmented answers grounded in a Markdown knowledge base
- Drafted customer replies with confidence scoring and guardrails
- Human approval, internal notes, and escalation review
- SLA tracking, CSAT, and admin analytics
- Outbound notifications and CRM/helpdesk sync

## Stack

| Layer | Choice |
|---|---|
| Frontend | React + Vite, `lucide-react` |
| Backend | FastAPI + Uvicorn, SQLAlchemy, Pydantic |
| Auth | JWT (PyJWT), Argon2 hashing (pwdlib) |
| Database | SQLite by default, PostgreSQL via `DATABASE_URL` (Alembic) |
| Search | sentence-transformers embeddings + FAISS |
| Queue | Celery + Redis, eager by default so dev needs no broker |
| AI | Provider-agnostic gateway (mock / OpenAI / Azure OpenAI) |
| Observability | OpenTelemetry tracing, structured JSON logs |

## Quickstart

Requires Python 3.14+ and Node.js 18+. Docker is optional, for PostgreSQL.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Then in a second terminal:

```powershell
Push-Location frontend
npm install
npm run dev
```

Open **http://localhost:5173/** — use `localhost`, not `127.0.0.1`, since Vite
binds to the IPv6 loopback.

API docs are served at http://127.0.0.1:8000/docs and health at
`/health`. Startup takes about 10 seconds because the ML imports load up front.

New self-registered accounts start as customers; staff roles are granted by an
admin.

Run the tests:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## Configuration

Copy `.env.example` to `.env` and adjust. `.env` is gitignored; the example is
the committed reference of every supported variable.

AI panels stay inactive until `LLM_PROVIDER` is set. Use `mock` for local work
with no API key:

```powershell
$env:LLM_PROVIDER="mock"
$env:AUTO_RESPOND_ENABLED="true"
```

Under `APP_ENV=production` the app refuses to boot on `LLM_PROVIDER=mock`, a
missing or short `JWT_SECRET`, a missing `CHANNEL_WEBHOOK_SECRET`, a `localhost`
entry in `TRUSTED_HOSTS`, or a disabled login throttle.

## Roles

Three roles, each enforced server-side rather than just hidden in the UI:

| Role | Reach |
|---|---|
| `customer` | Own tickets, own comments, knowledge search |
| `agent` | Full ticket queue, internal notes, drafts, agent assist |
| `admin` | Analytics, audit logs, user management |

Customers see only their own tickets, never receive internal triage output, and
can only open tickets on the `web` channel — the channel selects the SLA
multiplier, so letting a caller choose it would let them pick their own
deadline.

## Deployment

One image serves both roles, because `requirements.txt` pulls in torch via
sentence-transformers and a second image would duplicate that layer. The API is
the default command; the worker overrides it.

**Migrations are a separate step, not part of startup.** `create_all()` creates
missing tables but never alters an existing one, so an existing database needs
Alembic.

```powershell
docker build -t support-api .

docker run --rm -e DATABASE_URL=postgresql+psycopg://user:pass@host:5432/db support-api alembic upgrade head

docker run --rm -p 8000:8000 `
  -e DATABASE_URL=postgresql+psycopg://user:pass@host:5432/db `
  -e JWT_SECRET=your-long-random-secret `
  -e CHANNEL_WEBHOOK_SECRET=your-long-random-webhook-secret `
  -e APP_ENV=production `
  -e LLM_PROVIDER=openai `
  -e OPENAI_API_KEY=sk-... `
  -e TRUSTED_HOSTS=support.example.com `
  support-api

docker run --rm -e DATABASE_URL=... -e REDIS_URL=redis://redis:6379/0 `
  -e CELERY_TASK_ALWAYS_EAGER=0 `
  support-api celery -A app.core.workers worker --loglevel=info --pool=solo
```

Three deliberate choices in that setup: the worker uses `--pool=solo` because
Celery's prefork would copy a multi-gigabyte torch process per child; the
container runs non-root because it holds the database credentials and the
webhook secret; and `CELERY_TASK_ALWAYS_EAGER` must be `0`, since the default of
`1` runs tasks inline in the API and makes the worker a no-op.

## Operations

One JSON line per request on stdout with `method / path / status / duration_ms /
request_id`. OpenTelemetry spans are exported over OTLP/HTTP when
`OTEL_ENABLED=1`.

| Situation | What to do |
|---|---|
| A dependency is down | Every layer has a defined fallback: LLM to keyword rules, Celery to inline execution, and health checks report degraded rather than failing |
| Database is gone | Restore the backup, then `alembic upgrade head` **before** booting the API |
| Something is slow or wrong | Start with logs and `request_id`, then `GET /admin/llm/usage` |
| Audit evidence needed | `GET /admin/audit-logs`; purge past retention with `POST /admin/audit-logs/purge` |

## Testing

582 backend tests via pytest, plus a Playwright suite in `frontend/tests/`.
Coverage is measured over `app/` only and CI fails below 89%.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

CI runs four required jobs: `backend` (full suite, coverage floor, `pip-audit`),
`docker` (builds, asserts non-root, migrates an empty database, health check),
`e2e` (Playwright), and `postgres` (real PostgreSQL and Redis, plus a real broker
round-trip).

SQLite is the development default and it lies in three ways that matter: it does
not enforce foreign keys, its `TIMESTAMP` has no timezone, and an eager Celery
task "running" proves nothing about the queue. Point the suite at PostgreSQL with
`TEST_DATABASE_URL` to exercise the real thing.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `no such column: tickets.risk_score` | Migrations not applied — run `alembic upgrade head` |
| Blank page at `127.0.0.1:5173` | Use `localhost` (Vite binds to IPv6 loopback) |
| API port never opens with `--reload` | Restart without `--reload` |
| `400 Invalid host header` | `TRUSTED_HOSTS` needs the host you are using |
| Login throttled (429 + `Retry-After`) | 5 failed attempts per email+IP lock for 300 s |
| AI panels return 503 | `LLM_PROVIDER` unset — set it before starting the API |