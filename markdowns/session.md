# Project Session Log

Last updated: 2026-09-24 (Task 4 + Task 9 complete; remaining tasks 6, 7, 16)

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
- **LLM Gateway & Configuration (Task 1):** Provider-agnostic gateway in `app/llm.py` (`LLM_PROVIDER` env: mock/openai/azure_openai; opt-in — unset disables). Centralized `chat`/`generate`/`generate_json` entry points on raw HTTPS with injectable transport (no SDK dep); `MockLLMProvider` deterministic for dev/tests; OpenAI + Azure providers gated by creds. Process-local rate limiting (`LLM_RATE_LIMIT_PER_MINUTE`), retry-with-backoff on transient failures (`LLM_MAX_RETRIES`), token + estimated-cost tracking, redacted prompt/response JSON logging. Admin `GET /admin/llm/usage` + `POST /admin/llm/usage/reset` (audit-logged). Prompt template registry in `app/prompts.py`: named/versioned `triage.classify`, `response.draft`, `escalation.summary`, `agent_assist.suggest` with strict render validation. `validate_security_configuration` rejects unknown `LLM_PROVIDER` and fails production boot on `mock` or missing real-provider keys. New vars in `.env.example`. No migration needed.
- **Semantic Search via provider-aware embeddings (Task 2):** `app/embeddings.py` refactored to a provider-neutral `EmbeddingProvider` layer driven by `EMBEDDING_PROVIDER` (default `local` = sentence-transformers, unchanged behavior/API `semantic_search`/`build_index`/`ensure_index`; `openai` = OpenAI embeddings API, default `text-embedding-3-small`, requires `OPENAI_API_KEY`, batched via `EMBEDDING_BATCH_SIZE`). FAISS `IndexFlatIP` kept as the vector store. Cache (`data/embeddings`) now persists a provider+model signature and auto-rebuilds on any mismatch (old list-format caches invalidate and rebuild; handles 384→1536-dim switches). L2 normalization for cosine similarity + a dimension-mismatch guard returning `[]`. Recursive `validate_security_configuration` (unknown `EMBEDDING_PROVIDER` rejected; production OpenAI embeddings require a key). Vars documented in `.env.example`. No migration needed.
- **Response Agent with Guardrails (Task 3):** `app/response_agent.py` auto-drafts KB-grounded customer replies. `draft_reply()` retrieves top-K KB matches via `search_knowledge`, calls the LLM gateway with `response_format="json_object"` and the new versioned `response.draft_json` prompt template (draft + confidence + citations + escalate + reason), parses JSON (with `content` fallback), drops unmatched "Source N" citations, and derives confidence from the model (falling back to the best retrieval score when missing/out-of-range). Requires human review when no KB matches, empty draft, low confidence below `RESPONSE_CONFIDENCE_THRESHOLD` (default 0.75) or model escalation, or `validate_response` guardrail violations (e.g. placeholder text). `POST /tickets/{ticket_id}/response-draft` (staff-only) returns `DraftResult` + ticket_id, 503 on `LLMConfigError`/unset provider, 502 on other `LLMError`, audit-logged as `response.draft_generated`. Vars documented in `.env.example`. No migration needed.
- **Self-Service AI Auto-Respond (Task 3 extension):** `POST /tickets/{ticket_id}/auto-respond` (staff-only) + intake automation that answers customers automatically when safe and hands off otherwise. `run_auto_response()` calls `draft_reply()`; if `needs_review` is false it posts a customer-visible "Relay AI" comment (author `ai-assistant`), sets `first_response_at` + `resolved_at`, resolves the ticket, and audits `response.auto_sent` (deflection); otherwise it escalates to human review (`pending` status, `requires_human_review`, `escalation_status=pending`, audits `response.auto_escalated`). Tickets already flagged at intake are skipped (`response.auto_skipped`), LLM config/provider errors are audited and never break intake (~200 with `action: skipped`). Wired into `create_ticket` + `/webhooks/{channel}` behind opt-in `AUTO_RESPOND_ENABLED` (default false), documented in `.env.example`. Frontend: "Relay AI" label + `ai-tag` badge on AI comments in the conversation view, and an **Auto-respond** button with `auto_sent`/`needs_review`/`skipped` status banner on the staff AI Draft panel. Fixed a latent backend bug: `search_knowledge` could emit slightly negative cosine scores that violated `KnowledgeMatch.score >= 0` and crashed the draft flow (now clamped to `>= 0`). Fixed a frontend crash (white screen) found during live testing: the AI Draft card called `draft.ticket_id.slice(0, 8)` but the auto-respond response's nested `DraftResult` has no `ticket_id` — the meta line now guards it and the Auto-respond handler stamps `ticket_id` onto the rendered draft. No migration needed.
- **Draft approval workflow (Task 9 completion):** `POST /tickets/{ticket_id}/draft-decision` (staff-only) accepting `{decision: approve|reject, body?, note?, resolve?}`. Approve posts the reviewed draft as a public staff comment, sets `first_response_at`, resolves the ticket (optional via `resolve`, default true — clears `requires_human_review`/escalation), pushes to outbound integrations, audited as `response.draft_approved`. Reject optionally adds an internal note (`AI draft rejected: <reason>`), leaves the ticket for a human, audited as `response.draft_rejected`. 422 when approving without a body. Frontend AI Draft panel: "Approve & send" / "Reject draft" buttons with a "Resolve ticket on approve" toggle. Full backend suite `163 passed`; frontend production build passes.

## Current Task

### Task 4 (LLM-based Triage Classification) — completed 2026-09-24

- Replaced keyword-only triage with LLM `generate_json` using `triage.classify` prompt template (`app/prompts.py`).
- Falls back to keyword rules when `LLM_PROVIDER` is unset or LLM call fails (degraded gracefully).
- `classify_ticket()` in `app/triage.py` now calls `_classify_with_llm()` first, then `_classify_with_keywords()` on `LLMNotConfigured`/`LLMError`.
- All 156 backend tests pass.

## Validation History

- Backend suite: `74 passed` (was 37 at project start; feedback + guardrail engine + celery workers added ~37 tests)
- **Backend suite (Task 12): `79 passed`** (threading + dedup + channel SLA added 5 tests)
- **Backend suite (Task 13): `81 passed`** (analytics dashboard added 2 tests); frontend production build passes
- **Backend suite (Task 14): `92 passed`** (integration layer added 11 tests: 6 API-level + 5 adapter-level)
- **Backend suite (Task 15): `99 passed`** (production hardening added 7 tests: audit purge, request-id correlation, provider env validation, observability unit)
- **Backend suite (Task 1): `124 passed`** (LLM gateway added 25 tests: 19 in `test_llm.py` — providers, payloads, retries, rate limit, usage, JSON, prompts — + 6 config/endpoint tests in `test_main.py`)
- **Backend suite (Task 2): `135 passed`** (provider-aware embeddings added 11 tests: 9 in `test_embeddings.py` — provider payloads, batching, normalization, registry, cache signature invalidation, dimension guard — + 2 config tests in `test_main.py`)
- **Backend suite (Task 3): `149 passed`** (response agent added 14 tests: 10 in `test_response_agent.py` — auto-reply ready, low confidence, threshold override, escalation, guardrail violations, no-KB fallback, invalid JSON, retrieval-score confidence fallback, unmatched citations — + 4 endpoint tests in `test_main.py`)
- **Backend suite (Task 3 auto-respond): `156 passed`** (self-service automation added 7 tests: auto-send w/ AI comment + resolve, escalate-to-human, skip intake-flagged, staff-only 403, LLM-unset skip, intake enabled/disabled)
- **Backend suite (Task 9 completion): `163 passed`** (draft-decision endpoint added 7 tests: approve+resolve, approve-no-resolve, reject w/ note, reject no-note, approve-without-body 422, staff-only 403, unknown ticket 404)
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

Tracked in `REMAINING_TASKS.md`. Still pending: **Task 6** (escalation intelligence), **Task 7** (agent assist), **Task 16** (full codebase review — kept last). Task 9 (Human Review Dashboard + approve/reject AI drafts) is now fully complete.

## Next Priority Tasks (from REMAINING_TASKS.md)

**All remaining tasks (pending as of 2026-09-24):**

| Order | Task | Agent |
|-------|------|-------|
| 6 | **Build Escalation Intelligence** — risk via sentiment + intent + tier + SLA breach; auto-route with context summary. | Escalation Agent |
| 7 | **Implement Agent Assist Features** — summarize history, similar cases, reply templates, KB suggestions. | Agent Assist Agent |
| 16 | **Full Codebase Review & Simplification** (no LLM deps — can start anytime) | All |

DONE (not remaining): Tasks 1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 13, 14, 15. Tasks 1–4 (LLM gateway, provider-aware embeddings/FAISS, KB-grounded response drafts w/ guardrails, LLM-based triage) landed as the RAG foundation. Task 9 (Human Review Dashboard + explicit approve/reject of AI drafts w/ KB citations via `POST /tickets/{ticket_id}/draft-decision`) is fully complete. LLM-dependent tasks still remaining are 6, 7.

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
- **Task 1 (LLM Gateway & Configuration) completed — full backend suite `124 passed`; LLM gateway is opt-in (`LLM_PROVIDER`, mock default for dev), production boot refuses `mock` or missing real-provider keys**
- **Task 2 (Semantic Search: provider-aware embeddings + FAISS) completed — full backend suite `135 passed`; default `local` embeddings keep zero-key dev, `EMBEDDING_PROVIDER=openai` switches to `text-embedding-3-small` with automatic cache rebuild**
- **Task 3 (Response Agent with Guardrails) completed — full backend suite `149 passed`; `POST /tickets/{ticket_id}/response-draft` drafts KB-grounded replies with citations + confidence + guardrail checks before human review; mock provider enables zero-key demo, real providers via `LLM_PROVIDER`**
- **Task 3 self-service automation completed — full backend suite `156 passed`; `AUTO_RESPOND_ENABLED=true` lets the bot answer customers automatically (safe drafts → sent + resolved) and escalate everything else to humans; try it in the UI via the AI Draft panel's Auto-respond button or by creating a ticket with the env flag on (run server with `LLM_PROVIDER=mock` for zero-key demo)**
- **Task 4 (LLM-based Triage Classification) completed — full backend suite `156 passed`; `classify_ticket()` uses `generate_json` with `triage.classify` prompt template, falls back to keyword rules when `LLM_PROVIDER` unset or LLM call fails; no migration needed.**
- **Task 9 (Human Review Dashboard + draft approve/reject) completed — full backend suite `163 passed`; `POST /tickets/{ticket_id}/draft-decision` lets staff approve (sends reviewed draft as public reply, optional resolve) or reject (optional internal note) an AI draft with audit logging; frontend AI Draft panel gains Approve & send / Reject buttons + resolve-on-approve toggle; frontend production build passes.**
- **Live UI verified (2026-09-24):** Generate AI draft + Auto-respond work in the browser with `LLM_PROVIDER=mock`; fixed the white-screen crash (AI Draft card read `draft.ticket_id` which the auto-respond `DraftResult` lacks). Reminder for future dev sessions: no `.env` auto-loading — set `$env:LLM_PROVIDER="mock"`, `$env:AUTO_RESPOND_ENABLED="true"` in the terminal that launches uvicorn, and kill any stale process still holding port 8000 before restart.

## Working Rules

- Work step by step.
- Ask before implementing a new task unless approval is already clear.
- Run focused validation immediately after each edit.
- Update this file whenever a task is completed, a blocker is found, or the plan changes.
- Keep the final simplification review last.