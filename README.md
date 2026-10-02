# AI Customer Support Automation

A customer support platform that triages incoming tickets, drafts replies from a
knowledge base, and escalates anything risky to a human agent.

Every AI feature degrades gracefully. With no `LLM_PROVIDER` set, the system runs
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

Then, in a second terminal:

```powershell
Push-Location frontend
npm install
npm run dev
```

Open **http://localhost:5173/** — use `localhost`, not `127.0.0.1`, since Vite
binds to the IPv6 loopback.

To try it with realistic data:

```powershell
.\.venv\Scripts\python.exe scripts\seed_demo_data.py
```

Run the tests:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Full setup notes, demo credentials, troubleshooting, and the Docker and
PostgreSQL paths are in [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md).

## Configuration

Copy `.env.example` and adjust. AI panels stay inactive until
`LLM_PROVIDER` is set; use `mock` for local work with no API key.

Three roles, each enforced server-side:

| Role | Reach |
|---|---|
| `customer` | Own tickets, own comments, knowledge search |
| `agent` | Full ticket queue, internal notes, drafts, agent assist |
| `admin` | Analytics, audit logs, user management |

## Documentation

| Document | Covers |
|---|---|
| [Getting Started](docs/GETTING_STARTED.md) | Setup, demo data, troubleshooting |
| [Architecture](docs/ARCHITECTURE.md) | Layers, request flow, design decisions |
| [API Reference](docs/API_REFERENCE.md) | Endpoints, request and response shapes |
| [AI Agents](docs/AI_AGENTS.md) | Gateway, JSON contracts, per-agent policies |
| [Data Model](docs/DATA_MODEL.md) | Tables and relationships |
| [Configuration](docs/CONFIGURATION.md) | Every environment variable |
| [Deployment](docs/DEPLOYMENT.md) | Docker, migrations, production checklist |
| [Operations](docs/OPERATIONS.md) | Health checks, runbooks, audit log |
| [Testing](docs/TESTING_QUALITY.md) | Test strategy and coverage |