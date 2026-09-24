# Project Session Log

Last updated: 2026-09-24

## Purpose

AI-powered customer support ticketing system with human oversight, structured triage, knowledge retrieval, and agent workflows.

## Completed

- FastAPI backend with health and ticket APIs
- Ticket lifecycle: open, in_progress, pending, resolved, closed
- Ticket assignment, filtering, retrieval, and comments
- SQLAlchemy persistence with SQLite fallback
- PostgreSQL configuration and Docker Compose service
- Alembic migrations through `0009_add_guardrail_fields`
- Customer, agent, and admin roles
- Argon2 password hashing and registration
- JWT login and current-user authentication
- Role-based ticket permissions
- Admin staff-user creation, listing, activation, and deactivation
- React/Vite authentication UI
- Customer ticket portal and agent/admin dashboard
- Admin Team management panel
- Audit-log model, migration, event recording, API, and UI
- Structured triage with intent, priority, sentiment, confidence, team, summary, and review decision
- Structured triage persistence through migration `0004_add_structured_triage_fields`
- Markdown knowledge base and ranked authenticated retrieval endpoint
- Human escalation states and staff approval/rejection workflow
- SLA deadlines based on priority
- First-response and resolution timestamp tracking
- Admin SLA metrics endpoint
- Provider-neutral channel webhooks for email, chat, Slack, WhatsApp, and CRM messages
- Shared-secret webhook authentication and normalized channel ticket intake
- Production startup guard for JWT and webhook secrets
- Environment-configurable CORS origins
- Configurable trusted-host middleware
- Process-local webhook rate limiting
- **Semantic search with FAISS + sentence-transformers (all-MiniLM-L6-v2)**
- **Intake Normalization Layer (Task 5):** Channel adapters for email, Slack, WhatsApp, chat, CRM with legacy format compatibility; `intake_metadata` JSON column storing external_id, thread_id, attachments, channel_metadata, customer_context (tier, open_tickets, history, tags); customer context enrichment via `enrich_customer_context()`
- **Human Review Dashboard - Frontend (Task 9):** EscalationReviewPanel component with queue/list view, detail panel showing triage summary, SLA status, channel metadata; approve/reject workflow with reason; integrated into sidebar navigation for agents/admins; SLA visibility (badge + overdue highlight + sort/filter via shared `slaStatus`/`slaMinutesLeft`/`isTicketSlaOverdue` helpers in `frontend/src/main.jsx`)
- **Evaluation & Feedback Loop (Task 8):** `POST /tickets/{ticket_id}/feedback` + `feedback` table (migration `0008_create_feedback_table`); deflected = resolved with no agent response and no human review; `GET /admin/analytics/feedback`
- **Guardrail/Policy Engine (Task 11):** Deterministic engine in `app/guardrails.py` — PII regex (email/phone/SSN/credit-card Luhn/IPv4), compliance/legal keyword rules, policy blocklist (`BLOCKED_EMAIL_DOMAINS`) + high-value refunds (`HIGH_VALUE_REFUND_MIN_USD`, default 500), response validation hooks (`validate_response`) for future AI drafts, `redact()`. Wired into intake (`/tickets` + `/webhooks/{channel}`): flags + audits + auto-escalates to pending human review on high risk. Columns `guardrail_status`/`guardrail_hits` via migration `0009_add_guardrail_fields`; staff-only "flagged" badge + warnings panel in the UI
- **Async Job Queue (Task 10):** Celery app in `app/workers.py` with Redis broker/backend; `support.notify` task POSTs to `NOTIFY_WEBHOOK_URL` with 3 retries (30s backoff, stdlib `urllib`); eager-by-default (`CELERY_TASK_ALWAYS_EAGER=1`) so SQLite/no-Redis dev and tests need no broker — production sets `0` and runs `celery -A app.workers:celery_app worker`. Wired into intake (`ticket.created` / `ticket.received` events); create contract stays synchronous
- **Multi-Channel Orchestration (Task 12):** Threading via `thread_id` — incoming channel replies join the newest open ticket (same channel) as a customer comment; `pending → open` on customer reply; resolved/closed threads create a fresh ticket keeping the thread context. Idempotent dedup via `external_id` (`(channel, external_id)`), returning 200 + the recorded ticket for retries (tickets and replies). Channel-aware SLA via `SLA_CHANNEL_MULTIPLIERS` env (default email/web/crm 1.0, slack 0.75, chat/whatsapp 0.5) applied in `sla_deadline()`. New indexed columns `tickets.external_id`, `tickets.thread_id`, `ticket_comments.external_id` via migration `0010_add_channel_threading_fields`. Risky replies guardrail-checked and auto-escalate (`channel.thread_reply` audit). Also fixed latent JSON bug: `customer_context` persisted with `model_dump(mode="json")` (datetimes are now ISO strings).
- **Analytics Dashboard (Task 13):** Aggregated `GET /admin/analytics/dashboard` (admin-only) returning `SlaBreakdown` (total/open/in_progress/pending/overdue/resolved + avg resolution hours), `FeedbackAnalytics` CSAT, `EscalationAnalytics` (total/pending/approved/rejected + rate), team `workload` per assignee, channel/priority/intent/sentiment value counts, and a triage `confidence` histogram (low/medium/high/very_high + average). Frontend `AnalyticsView` panel: stat cards (avg resolution, overdue SLA, avg rating, deflection/response rate, escalation rate, pending review) + bar breakdowns via shared `BreakdownBars`; admin "Analytics" sidebar nav item; no chart library added; new `.analytics-*` styles.
- **CRM/Helpdesk Integrations (Task 14):** Provider-agnostic outbound sync layer in `app/integrations.py` — `HelpdeskProvider` ABC (+ mock/http injection for tests), `MockHelpdeskProvider` (credential-free, stateless), `ZendeskProvider` and `HubSpotProvider` (gated by API creds; `IntegrationError` until configured), registry + `get_provider()` driven by `SUPPORT_TOOL_PROVIDER` env (default unset = outbound sync disabled/opt-in). Wired outbound create/update (create_ticket, webhook, update_ticket) and comment push (add_comment; public only) in `main.py`; sync state persisted to `ticket.intake_metadata.integration` and `integration.synced` audit events. Comment push skips with reason `no_remote_ticket_id` when the ticket was never synced. `mock` is recommended for dev/demo; new vars documented in `.env.example`. No new migration required.

## Current Task

### Production security

First slice completed:

- Production startup rejects missing or local default JWT secrets
- Production startup rejects missing or local default webhook secrets
- CORS origins are configured through `CORS_ORIGINS`

Additional hardening is still pending.

The current webhook limiter is intentionally process-local; distributed deployments should move this state to Redis or another shared store.

### Channel integrations

First slice completed:

- `POST /webhooks/{channel}` for supported channel names
- `X-Webhook-Secret` validation
- Normalization into the existing ticket, triage, escalation, SLA, and audit pipeline

Provider SDKs and outbound delivery are still pending.

## Validation History

- Backend suite: `74 passed` (was 37 at project start; feedback + guardrail engine + celery workers added ~37 tests)
- **Backend suite (Task 12): `79 passed`** (threading + dedup + channel SLA added 5 tests)
- **Backend suite (Task 13): `81 passed`** (analytics dashboard added 2 tests); frontend production build passes
- **Backend suite (Task 14): `92 passed`** (integration layer added 11 tests: 6 API-level + 5 adapter-level)
- **Backend suite (Task 15): `99 passed`** (production hardening added 7 tests: audit purge, request-id correlation, provider env validation, observability unit)
- Frontend production build passes
- Frontend e2e tests: `2 passed` (auth + ticket lifecycle, cross-customer access control)
- **Semantic search: FAISS index built, cosine similarity retrieval working**
- Alembic migration `0007_add_intake_metadata` upgrades to head and downgrades successfully
- Alembic `0008_create_feedback_table` and `0009_add_guardrail_fields` applied to dev DB (`support.db`) with `alembic stamp 0008_create_feedback_table` (feedback table pre-created by `create_all`) then `alembic upgrade head`
- **Alembic `0010_add_channel_threading_fields` applied to dev DB (`support.db`); new columns verified via PRAGMA**

### Production Hardening (Task 15) (completed 2026-09-24)

- **Observability:** `app/observability.py` — structured JSON logging (`LOG_LEVEL`), `RequestContextMiddleware` (per-request `X-Request-Id` echoed + correlated into log lines via `contextvars`), and opt-in OpenTelemetry tracing (`OTEL_ENABLED=1` + `OTEL_EXPORTER_OTLP_ENDPOINT`) with `FastAPIInstrumentor`; wired into `main.py`. Deps: `opentelemetry-{api,sdk,exporter-otlp-proto-http,instrumentation-fastapi}`.
- **Load testing:** `load_tests/locustfile.py` — `CustomerUser` (register/login/tickets/knowledge-search/auth-me) + `AdminUser` (analytics dashboard/audit-logs/sla-metrics, gated on `ADMIN_EMAIL`/`ADMIN_PASSWORD`). Baseline committed: 177 requests / 0 errors in 60s at ~3 rps (SQLite, dev); register ~2.4s (argon2), steady-state p95s all <400ms. `load_tests/README.md` + `reports/baseline.html`.
- **Config hardening:** `validate_security_configuration` now fails fast on unknown `SUPPORT_TOOL_PROVIDER`; `AUDIT_LOG_RETENTION_DAYS` (default 365) + admin-only `POST /admin/audit-logs/purge`.
- **Runbooks:** `markdowns/runbooks/` — `RUNBOOK-observability`, `RUNBOOK-secrets-management`, `RUNBOOK-disaster-recovery`, `RUNBOOK-audit-compliance`, `RUNBOOK-chaos-engineering`.
- Deployed/verified: dev server ran under load and was stopped; no schema changes required.

### Intake Normalization Layer (completed 2026-09-24)

- Created `app/intake.py` with NormalizedMessage, Attachment, CustomerContext models
- 5 channel adapters: Email, Slack, WhatsApp, Chat, CRM with provider-specific field extraction
- Backward compatible with legacy ChannelMessage format
- Migration `0007_add_intake_metadata` adds `intake_metadata` JSON column to tickets
- Webhook endpoint updated to use normalization layer
- Customer context enrichment (tier, open tickets, history, tags)

### Human Review Dashboard - Frontend (completed 2026-09-24)

- EscalationReviewPanel React component with split-pane queue/detail view
- Displays triage summary, SLA countdown, channel metadata (intake_metadata)
- Approve/Reject workflow with required reason field
- Sidebar navigation: Inbox, Escalations (agents/admins), Conversations
- Frontend production build succeeds
- E2E tests pass (auth + ticket lifecycle, cross-customer access control)

## Remaining Tasks

1. Add provider-specific channel adapters and outbound delivery when provider credentials/contracts are available

## Next Priority Tasks (from REMAINING_TASKS.md)

| Order | Task | Agent |
|-------|------|-------|
| — | *(Tasks 12, 13, 14, 15 — see REMAINING_TASKS.md)* | — |

LLM-dependent tasks (1–4, 6–7) still pending, as are the AI-draft review parts of Task 9 (blocked on Tasks 3/7).

## Final Review Notes

- Backend suite: `74 passed`
- Frontend production build succeeds
- Frontend e2e tests: `2 passed`
- Python diagnostics report no errors
- Provider-neutral webhooks are complete; vendor-specific integrations require external credentials and payload contracts
- Current security baseline is suitable for local development and has production secret checks, trusted hosts, configurable CORS, and webhook throttling
- Guardrail engine is deterministic (regex/keyword/Luhn) per AGENTS.md policy; designed as the validation layer a future LLM response pipeline must pass through
- Job queue is eager-by-default so the existing zero-infra dev setup keeps working; production needs Redis + `CELERY_TASK_ALWAYS_EAGER=0` + a worker process
- Dev DB note: `init_db()` (create_all) does not apply Alembic migrations; run `alembic upgrade head` after pulling new migrations
- README now contains a complete Windows/PowerShell run guide for SQLite, PostgreSQL, API, frontend, tests, and environment variables
- PowerShell end-to-end API smoke test added at `scripts/smoke_test.ps1`
- **Task 5 (Intake Normalization), Task 8 (Feedback Loop), Task 9 (Human Review Dashboard + SLA visibility), Task 10 (Async Job Queue), Task 11 (Guardrail/Policy Engine) completed**
- **Task 12 (Multi-Channel Orchestration), Task 13 (Analytics Dashboard), Task 14 (CRM/Helpdesk Integrations), Task 15 (Production Hardening) completed — full backend suite `99 passed`**

## Working Rules

- Work step by step.
- Ask before implementing a new task unless approval is already clear.
- Run focused validation immediately after each edit.
- Update this file whenever a task is completed, a blocker is found, or the plan changes.
- Keep the final simplification review last.