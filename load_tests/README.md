# Load Testing (Locust)

Scenario suite for the API in `locustfile.py`.

## Setup

```powershell
.\.venv\Scripts\python.exe -m pip install locust
```

## Run the API under test

```powershell
$env:APP_ENV="development"
$env:SUPPORT_TOOL_PROVIDER="mock"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

## Run a quick smoke test (10 users, 1 minute)

```powershell
$env:ADMIN_EMAIL="load-admin@example.com"
$env:ADMIN_PASSWORD="loadtest-pass-1"
.\.venv\Scripts\python.exe -m locust -f load_tests\locustfile.py `
  --host http://localhost:8000 --headless -u 10 -r 2 --run-time 1m `
  --html load_tests\reports\baseline.html
```

Interactive web UI (recommended for tuning): `--web-port 8089`.

## Scenarios

- **CustomerUser** (`/auth/register`, `/auth/login`, `/tickets` POST/GET,
  `/knowledge/search`, `/auth/me`) — emulates end-user traffic across channels.
- **AdminUser** (`/admin/analytics/dashboard`, `/admin/audit-logs`,
  `/admin/analytics/sla`) — evaluates reporting surface under concurrent load.
  Bootstrap an admin user and export `ADMIN_EMAIL`/`ADMIN_PASSWORD`; tasks are
  skipped when these are unset.

## Baseline (2026-09-24)

Environment: Windows, SQLite (`support.db`), single uvicorn worker, `SUPPORT_TOOL_PROVIDER=mock`,
4 customer + 4 admin users, 60s steady run (~3 req/s total). Report: `reports/baseline.html`.

| Endpoint | Requests | Avg (ms) | p50 (ms) | p95 (ms) | Failures |
|----------|----------|----------|----------|----------|----------|
| POST /tickets | 38 | 176 | 160 | 360 | 0 |
| GET /knowledge/search | 25 | 38 | 27 | 89 | 0 |
| GET /tickets | 28 | 21 | 7 | 130 | 0 |
| GET /admin/analytics/dashboard | 29 | 20 | 9 | 110 | 0 |
| GET /admin/audit-logs | 20 | 29 | 10 | 200 | 0 |
| GET /admin/analytics/sla | 14 | 16 | 9 | 76 | 0 |
| GET /auth/me | 11 | 13 | 5 | 99 | 0 |
| POST /auth/login | 4 | 177 | 190 | 190 | 0 |
| POST /auth/register | 4 | 2442 | 2500 | 2600 | 0 |

Notes:
- `/auth/register` dominates latency (~2.4s) due to argon2 password hashing; it is a
  one-time setup step per user, not part of the steady-state traffic path.
- SQLite serializes writes; expect materially lower write latency on Postgres.
- Compare future runs against this table with identical scenario parameters.

## Guidelines

- Baseline before optimization: capture a CSV/HTML report, then compare against
  the same scenario after changes.
- Track p50/p95 latency, RPS, and error rate; the dev server is not
  representative of Postgres + Redis + Celery production.
- Keep `CELERY_TASK_ALWAYS_EAGER=1` (default) for API-only load; enable a
  worker + broker for end-to-end queue latency testing.